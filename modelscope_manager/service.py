from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlparse

from .transfer_policy import SharedRateLimiter


SUPPORTED_REPO_TYPES = ("model", "dataset")
REPOSITORY_PAGE_SIZE = 50
MAX_MODEL_UPLOAD_FILE_SIZE = 50 * 1024**3


_UPLOAD_LIMITER = SharedRateLimiter()


def configure_upload_limit_supplier(supplier: Callable[[], int]) -> None:
    """Install one process-wide aggregate limiter into the bundled SDK stream.

    The SDK can upload folder members on worker threads, so limiting the shared
    counted stream keeps the configured value aggregate rather than per file.
    """
    global _UPLOAD_LIMITER
    _UPLOAD_LIMITER = SharedRateLimiter(supplier)
    import modelscope_hub._upload as upload_module

    stream_class = upload_module._CountedReadStream
    if getattr(stream_class, "_modelscope_manager_limited", False):
        return
    original_read = stream_class.read

    def limited_read(stream, size: int = -1):
        chunk = original_read(stream, size)
        _UPLOAD_LIMITER.throttle(len(chunk))
        return chunk

    stream_class.read = limited_read
    stream_class._modelscope_manager_limited = True

    manager_class = upload_module.UploadManager
    if not getattr(manager_class, "_modelscope_manager_normal_limited", False):
        original_build_operation = manager_class._build_operation

        def limited_build_operation(manager, *args, **kwargs):
            operation = original_build_operation(manager, *args, **kwargs)
            if operation.get("type") == "normal":
                _UPLOAD_LIMITER.throttle(int(operation.get("size", 0)))
            return operation

        manager_class._build_operation = limited_build_operation
        manager_class._modelscope_manager_normal_limited = True


def parse_modelscope_repository_url(value: str) -> "Repository":
    """Parse a public ModelScope model/dataset page into a repository identity."""
    raw = value.strip()
    if not raw:
        raise ValueError("请输入 ModelScope 数据集或模型链接")
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = (parsed.hostname or "").lower()
    if host not in {"modelscope.cn", "www.modelscope.cn", "modelscope.ai", "www.modelscope.ai"}:
        raise ValueError("仅支持 ModelScope 官方链接")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0].lower() not in {"datasets", "models"}:
        raise ValueError("链接格式应为 /datasets/账户/仓库 或 /models/账户/仓库")
    repo_type = "dataset" if parts[0].lower() == "datasets" else "model"
    owner, name = parts[1].strip(), parts[2].strip()
    if not owner or not name or owner in {".", ".."} or name in {".", ".."}:
        raise ValueError("链接中缺少账户或仓库名称")
    return Repository(f"{owner}/{name}", repo_type, "public")


def normalize_remote_path(*parts: str) -> str:
    """Join user-entered repository paths without allowing traversal."""
    clean: list[str] = []
    for raw in parts:
        raw = (raw or "").replace("\\", "/")
        for part in PurePosixPath(raw).parts:
            if part in ("", ".", "/"):
                continue
            if part == "..":
                raise ValueError("仓库路径不能包含 '..'")
            clean.append(part.strip("/"))
    return "/".join(filter(None, clean))


def repository_directories(paths: Iterable[str]) -> set[str]:
    """Infer directory nodes from a flat repository tree response."""
    directories: set[str] = set()
    for raw in paths:
        parts = [part for part in raw.replace("\\", "/").split("/") if part]
        for index in range(1, len(parts)):
            directories.add("/".join(parts[:index]))
    return directories


class _CallbackTqdm:
    """Minimal tqdm-compatible object used to surface SDK byte progress."""

    def __init__(self, iterable=None, *, total=None, unit=None, callback=None, **kwargs):
        self.iterable = iterable
        self.total = total
        self.unit = unit
        self.callback = callback if unit == "B" else None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def __iter__(self):
        return iter(self.iterable or ())

    def update(self, amount=1):
        if self.callback:
            self.callback(int(amount))

    def close(self):
        return None

    def set_postfix(self, *args, **kwargs):
        return None


@dataclass(frozen=True)
class Repository:
    repo_id: str
    repo_type: str
    visibility: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class RemoteEntry:
    path: str
    size: int = 0
    sha256: str = ""
    is_dir: bool = False


def oversized_upload_files(
    paths: Iterable[Path],
    limit: int = MAX_MODEL_UPLOAD_FILE_SIZE,
) -> list[Path]:
    oversized: list[Path] = []
    for path in paths:
        candidates = path.rglob("*") if path.is_dir() else (path,)
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                if candidate.stat().st_size > limit:
                    oversized.append(candidate.resolve())
            except OSError:
                continue
    return oversized


class ModelScopeService:
    """Small adapter around modelscope-hub so the GUI stays SDK-version agnostic."""

    def __init__(self, token: str = "", require_token: bool = True):
        token = token.strip()
        if require_token and not token:
            raise ValueError("请输入访问令牌")
        from modelscope_hub import HubApi

        self.api = HubApi(token=token)
        self.token = token
        self.user: Any | None = None

    def verify(self) -> str:
        self.user = self.api.whoami()
        username = getattr(self.user, "username", None)
        if not username:
            raise RuntimeError("令牌验证成功，但未能取得用户名")
        return str(username)

    def list_repositories(self, types: Iterable[str] = SUPPORTED_REPO_TYPES) -> list[Repository]:
        if self.user is None:
            self.verify()
        owner = str(getattr(self.user, "username", "")) or None
        output: list[Repository] = []
        for repo_type in types:
            page = 1
            while True:
                result = self.api.list_repos(
                    repo_type,
                    owner=owner,
                    page_number=page,
                    page_size=REPOSITORY_PAGE_SIZE,
                )
                items = list(getattr(result, "items", result or []))
                for item in items:
                    item_owner = str(getattr(item, "owner", "") or owner or "")
                    name = str(getattr(item, "name", ""))
                    repo_id = f"{item_owner}/{name}" if item_owner and name else str(getattr(item, "id", ""))
                    if not repo_id:
                        continue
                    visibility = str(getattr(item, "visibility", "") or ("private" if getattr(item, "private", False) else "public"))
                    updated = str(getattr(item, "last_modified", "") or "")
                    output.append(Repository(repo_id, repo_type, visibility, updated))
                total = int(getattr(result, "total_count", len(items)) or len(items))
                if not items or page * REPOSITORY_PAGE_SIZE >= total:
                    break
                page += 1
        return sorted(output, key=lambda value: (value.repo_type, value.repo_id.lower()))

    def list_entries(self, repo: Repository) -> list[RemoteEntry]:
        if repo.repo_type == "dataset" and hasattr(self.api.legacy, "list_dataset_files_paginated"):
            # The generic dataset tree endpoint returns only its first page.
            # Use the SDK's paginator so large repositories are not truncated.
            files = self.api.legacy.list_dataset_files_paginated(
                repo.repo_id,
                page_size=100,
            )
        else:
            files = self.api.list_repo_files(repo.repo_id, repo.repo_type, recursive=True)

        entries: dict[str, RemoteEntry] = {}
        for item in files:
            if isinstance(item, dict):
                path = item.get("Path") or item.get("path")
                entry_type = str(item.get("Type") or item.get("type") or "").lower()
                size = int(item.get("Size") or item.get("size") or 0)
                sha256 = str(item.get("Sha256") or item.get("sha256") or "")
            else:
                path = getattr(item, "path", None)
                entry_type = str(getattr(item, "type", "") or "").lower()
                size = int(getattr(item, "size", 0) or 0)
                sha256 = str(getattr(item, "sha256", "") or "")
            if path:
                normalized = str(path).replace("\\", "/").strip("/")
                entries[normalized] = RemoteEntry(
                    path=normalized,
                    size=size,
                    sha256=sha256,
                    is_dir=entry_type in {"tree", "directory", "dir", "folder"},
                )
        return sorted(entries.values(), key=lambda entry: entry.path)

    def list_files(self, repo: Repository) -> list[str]:
        return [entry.path for entry in self.list_entries(repo)]

    def get_download_url(self, repo: Repository, remote_path: str) -> str:
        return self.api.legacy.get_download_url(
            repo.repo_id,
            repo.repo_type,
            remote_path,
            "master",
        )

    @contextmanager
    def track_upload_progress(self, callback: Callable[[int], None]):
        """Temporarily bridge the bundled SDK's byte counter to the GUI."""
        import modelscope_hub._upload as upload_module

        original_tqdm = upload_module.tqdm

        def progress_factory(iterable=None, **kwargs):
            return _CallbackTqdm(iterable, callback=callback, **kwargs)

        upload_module.tqdm = progress_factory
        try:
            yield
        finally:
            upload_module.tqdm = original_tqdm

    def upload_file(self, repo: Repository, local_path: Path, target_folder: str) -> Any:
        remote_path = normalize_remote_path(target_folder, local_path.name)
        return self.upload_file_as(repo, local_path, remote_path)

    def upload_file_as(self, repo: Repository, local_path: Path, remote_path: str) -> Any:
        remote_path = normalize_remote_path(remote_path)
        return self.api.upload_file(
            repo.repo_id,
            repo.repo_type,
            str(local_path),
            remote_path,
            commit_message=f"Upload {local_path.name} via ModelScope Manager",
            disable_tqdm=True,
        )

    def upload_folder(
        self,
        repo: Repository,
        local_path: Path,
        target_folder: str,
        keep_folder_name: bool = True,
        ignore_patterns: list[str] | None = None,
    ) -> Any:
        remote_folder = normalize_remote_path(
            target_folder,
            local_path.name if keep_folder_name else "",
        )
        return self.api.upload_folder(
            repo.repo_id,
            repo.repo_type,
            str(local_path),
            path_in_repo=remote_folder,
            commit_message=f"Upload folder {local_path.name} via ModelScope Manager",
            ignore_patterns=ignore_patterns,
            disable_tqdm=True,
        )


class MultiAccountService:
    """Route repository operations to the verified account that owns each repo."""

    def __init__(
        self,
        services: dict[str, ModelScopeService],
        repositories: dict[str, list[Repository]],
    ):
        self.services = dict(services)
        self.account_repositories = {key: list(value) for key, value in repositories.items()}
        self._routes: dict[tuple[str, str], ModelScopeService] = {}
        for account_id, repos in self.account_repositories.items():
            service = self.services.get(account_id)
            if not service:
                continue
            for repo in repos:
                self._routes.setdefault((repo.repo_type, repo.repo_id), service)
        self.token = ""

    def list_repositories(self) -> list[Repository]:
        output: dict[tuple[str, str], Repository] = {}
        for repos in self.account_repositories.values():
            for repo in repos:
                output.setdefault((repo.repo_type, repo.repo_id), repo)
        return sorted(output.values(), key=lambda item: (item.repo_type, item.repo_id.lower()))

    def _for(self, repo: Repository) -> ModelScopeService:
        service = self._routes.get((repo.repo_type, repo.repo_id))
        if service is None:
            raise RuntimeError(f"No verified account can access {repo.repo_id}")
        return service

    def list_entries(self, repo: Repository) -> list[RemoteEntry]:
        return self._for(repo).list_entries(repo)

    def get_download_url(self, repo: Repository, remote_path: str) -> str:
        return self._for(repo).get_download_url(repo, remote_path)

    def upload_file_as(self, repo: Repository, local_path: Path, remote_path: str) -> Any:
        return self._for(repo).upload_file_as(repo, local_path, remote_path)


def upload_paths(
    service: ModelScopeService,
    repo: Repository,
    paths: Iterable[Path],
    target_folder: str,
    keep_folder_name: bool,
    on_item: Callable[[Path, bool, str], None] | None = None,
) -> tuple[int, int]:
    ok = failed = 0
    for path in paths:
        try:
            if path.is_dir():
                service.upload_folder(repo, path, target_folder, keep_folder_name)
            elif path.is_file():
                service.upload_file(repo, path, target_folder)
            else:
                raise FileNotFoundError(str(path))
        except Exception as exc:
            failed += 1
            if on_item:
                on_item(path, False, str(exc))
        else:
            ok += 1
            if on_item:
                on_item(path, True, "上传完成")
    return ok, failed
