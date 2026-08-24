from __future__ import annotations

import hashlib
import shutil
import subprocess
import uuid
from pathlib import Path


POTPLAYER_ARCHIVE_SIZE = 68_984_476
POTPLAYER_ARCHIVE_SHA256 = "4d58547cff31ec047eb26cc6ad86c2d98b0694ff48c79a7df29d415b6ad521ce"
POTPLAYER_REPOSITORY = "ARXChem/Animations-List"
POTPLAYER_REMOTE_PATH = "! Software/PotPlayer.7z"
POTPLAYER_EXECUTABLES = ("PotPlayerMini64.exe", "PotPlayerMini.exe", "PotPlayer.exe")


def verify_archive(path: Path) -> None:
    if not path.is_file() or path.stat().st_size != POTPLAYER_ARCHIVE_SIZE:
        raise ValueError("PotPlayer 压缩包大小校验失败")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest().lower() != POTPLAYER_ARCHIVE_SHA256:
        raise ValueError("PotPlayer 压缩包 SHA-256 校验失败")


def find_potplayer(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    for name in POTPLAYER_EXECUTABLES:
        matches = sorted(directory.rglob(name), key=lambda item: len(item.parts))
        if matches:
            return matches[0].resolve()
    return None


def install_potplayer(archive: Path, seven_zip: Path, destination: Path) -> Path:
    verify_archive(archive)
    if not seven_zip.is_file():
        raise FileNotFoundError(f"缺少 7z-zstd 解压工具：{seven_zip}")
    parent = destination.parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".potplayer-install-{uuid.uuid4().hex}"
    backup = parent / f".potplayer-backup-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        process = subprocess.run(
            [str(seven_zip), "x", str(archive), f"-o{staging}", "-y", "-bso0", "-bsp0"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=600,
        )
        if process.returncode != 0:
            raise RuntimeError(process.stdout.strip() or f"7z-zstd 返回代码 {process.returncode}")
        executable = find_potplayer(staging)
        if executable is None:
            raise FileNotFoundError("压缩包中未找到 PotPlayer 主程序")
        payload = executable.parent
        if destination.exists():
            destination.replace(backup)
        shutil.move(str(payload), str(destination))
        installed = find_potplayer(destination)
        if installed is None:
            raise FileNotFoundError("PotPlayer 解压后主程序不存在")
        if backup.exists():
            shutil.rmtree(backup)
        archive.unlink(missing_ok=True)
        return installed
    except Exception:
        if destination.exists() and backup.exists():
            shutil.rmtree(destination, ignore_errors=True)
        if backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
