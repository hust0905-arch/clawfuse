"""Container lifecycle management — pre-start and pre-destroy hooks."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .cache import ContentCache
from .client import DriveKitClient
from .config import Config, FOLDER_MIME
from .dirtree import DirTree
from .exceptions import MountError, TokenError
from .token import TokenManager
from .writebuf import FlushResult, WriteBuffer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MountResult:
    """Result of a pre-start mount operation."""

    success: bool
    mount_point: str
    file_count: int
    load_time_seconds: float
    error: str = ""


@dataclass(frozen=True)
class SyncResult:
    """Result of a pre-destroy sync operation."""

    files_synced: int
    files_failed: int
    errors: list[str]
    sync_time_seconds: float


@dataclass(frozen=True)
class StatusReport:
    """Current status of the ClawFUSE mount."""

    mounted: bool
    mount_point: str
    file_count: int
    cache_entries: int
    cache_bytes: int
    pending_writes: int
    uptime_seconds: float


class LifecycleManager:
    """Manages container lifecycle: startup mount and shutdown sync."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._token: TokenManager | None = None
        self._client: DriveKitClient | None = None
        self._dirtree: DirTree | None = None
        self._cache: ContentCache | None = None
        self._writebuf: WriteBuffer | None = None
        self._start_time: float = 0
        self._mounted: bool = False

    def pre_start(self) -> MountResult:
        """Initialize all components and mount FUSE.

        Called before the OpenClaw agent starts.
        """
        start = time.monotonic()
        try:
            # 1. Ensure directories exist
            self._config.ensure_dirs()

            # 2. Initialize token manager
            self._token = self._create_token_manager()
            # Validate token is readable
            _ = self._token.access_token

            # 3. Create Drive Kit client
            self._client = DriveKitClient(self._token, timeout=self._config.http_timeout)

            # 4. Resolve cloud_folder to folder ID if needed
            root_folder = self._resolve_root_folder(self._client)

            # 5. Initialize directory tree (lazy mode — no blocking BFS)
            self._dirtree = DirTree(
                self._client,
                root_folder=root_folder,
                refresh_ttl=self._config.tree_refresh_ttl,
            )

            # Start background full load (daemon thread, does not block mount)
            self._bg_thread = threading.Thread(
                target=self._dirtree.background_full_load,
                daemon=True,
                name="clawfuse-bg-load",
            )
            self._bg_thread.start()
            logger.info("Background metadata loading started")

            file_count = self._dirtree.file_count  # 0 at this point

            # 6. Initialize cache
            self._cache = ContentCache(
                cache_dir=self._config.cache_dir,
                max_bytes=self._config.cache_max_bytes,
                max_files=self._config.cache_max_files,
            )

            # 7. Initialize write buffer
            self._writebuf = WriteBuffer(
                client=self._client,
                buffer_dir=self._config.write_buf_dir,
                drain_interval=self._config.drain_interval,
                max_retries=self._config.drain_max_retries,
            )

            # 8. Start drain thread
            self._writebuf.start_drain()

            # 9. Mark as mounted (actual FUSE mount is separate, called from mount.py)
            self._mounted = True
            self._start_time = time.monotonic()

            # Update config's root_folder to the resolved ID
            # (frozen dataclass — we store it for get_fuse_ops)
            self._resolved_root_folder = root_folder

            elapsed = time.monotonic() - start
            logger.info(
                "ClawFUSE pre_start complete: %d files, %.2fs",
                file_count,
                elapsed,
            )

            return MountResult(
                success=True,
                mount_point=self._config.mount_point,
                file_count=file_count,
                load_time_seconds=elapsed,
            )

        except (TokenError, Exception) as e:
            elapsed = time.monotonic() - start
            logger.error("ClawFUSE pre_start failed: %s (%.2fs)", e, elapsed)
            return MountResult(
                success=False,
                mount_point=self._config.mount_point,
                file_count=0,
                load_time_seconds=elapsed,
                error=str(e),
            )

    def pre_destroy(self, timeout: float = 120.0) -> SyncResult:
        """Sync all pending writes and unmount.

        Called before the container is destroyed.
        """
        start = time.monotonic()
        logger.info("ClawFUSE pre_destroy: syncing pending writes")

        if self._writebuf is None:
            return SyncResult(
                files_synced=0,
                files_failed=0,
                errors=[],
                sync_time_seconds=0,
            )

        result = self._writebuf.flush_all(timeout=timeout * 0.8)
        elapsed = time.monotonic() - start

        self._mounted = False

        logger.info(
            "ClawFUSE pre_destroy complete: %d synced, %d failed in %.1fs",
            result.succeeded,
            result.failed,
            elapsed,
        )

        return SyncResult(
            files_synced=result.succeeded,
            files_failed=result.failed,
            errors=result.errors,
            sync_time_seconds=elapsed,
        )

    def status(self) -> StatusReport:
        """Get current status."""
        return StatusReport(
            mounted=self._mounted,
            mount_point=self._config.mount_point,
            file_count=self._dirtree.file_count if self._dirtree else 0,
            cache_entries=self._cache.entry_count if self._cache else 0,
            cache_bytes=self._cache.total_bytes if self._cache else 0,
            pending_writes=self._writebuf.pending_count if self._writebuf else 0,
            uptime_seconds=(time.monotonic() - self._start_time) if self._start_time else 0,
        )

    @property
    def is_mounted(self) -> bool:
        return self._mounted

    @property
    def client(self) -> DriveKitClient | None:
        return self._client

    @property
    def dirtree(self) -> DirTree | None:
        return self._dirtree

    @property
    def cache(self) -> ContentCache | None:
        return self._cache

    @property
    def writebuf(self) -> WriteBuffer | None:
        return self._writebuf

    @property
    def token(self) -> TokenManager | None:
        return self._token

    def get_fuse_ops(self) -> object | None:
        """Get the FUSE operations object for mounting."""
        if not all([self._client, self._dirtree, self._cache, self._writebuf]):
            return None

        from .fuse import ClawFUSE

        root_folder = getattr(self, "_resolved_root_folder", self._config.root_folder)

        return ClawFUSE(
            client=self._client,  # type: ignore[arg-type]
            dirtree=self._dirtree,  # type: ignore[arg-type]
            cache=self._cache,  # type: ignore[arg-type]
            writebuf=self._writebuf,  # type: ignore[arg-type]
            root_folder=root_folder,
        )

    def _create_token_manager(self) -> TokenManager:
        """Create TokenManager based on config mode."""
        if self._config.token_string:
            return TokenManager.from_string(self._config.token_string)
        if self._config.token_file is not None:
            return TokenManager.from_file(self._config.token_file)
        raise TokenError("No token configured — set token in config file or CLAWFUSE_TOKEN_FILE")

    def _resolve_root_folder(self, client: DriveKitClient) -> str:
        """Resolve cloud_folder to a Drive Kit folder ID.

        Three cases:
        1. 'applicationData' — discover the real root folder ID from the API.
           'applicationData' is a container name, not a parentFolder value.
           queryParam='applicationData' in parentFolder returns nothing.
        2. Folder name (short string) — look it up in the root directory.
        3. Folder ID (20+ chars) — use directly.
        """
        folder_name = self._config.cloud_folder

        # Case 1: applicationData — discover real root folder ID
        if folder_name == "applicationData":
            return self._discover_application_data_root(client)

        # Case 3: already an ID
        if not self._config.needs_folder_resolution:
            return self._config.root_folder

        # Case 2: folder name — resolve by listing root, auto-create if missing
        logger.info("Resolving cloud_folder '%s' to folder ID...", folder_name)

        root_id = self._discover_application_data_root(client)

        if root_id != "applicationData":
            # Normal path: root discovered, search for named folder
            result = client.list_files(parent_folder=root_id, page_size=100)
            for f in result.get("files", []):
                if f.get("fileName") == folder_name and f.get("mimeType") == FOLDER_MIME:
                    folder_id = f["id"]
                    logger.info("Resolved '%s' → %s", folder_name, folder_id)
                    return folder_id

        # Folder not found (or empty container) — auto-create
        logger.info(
            "Cloud folder '%s' not found, auto-creating under applicationData",
            folder_name,
        )
        created = client.create_folder(
            folder_name=folder_name,
            parent_folder=root_id,
        )
        folder_id = created.get("id", "")
        if not folder_id:
            raise MountError(f"Failed to auto-create folder '{folder_name}': {created}")

        logger.info("Auto-created folder '%s' → %s", folder_name, folder_id)
        return folder_id

    def _discover_application_data_root(self, client: DriveKitClient) -> str:
        """Discover the real root folder ID for the applicationData container.

        'applicationData' is a special container name in Drive Kit.
        queryParam='applicationData' in parentFolder acts as a keyword
        that lists root-level items of the container. From those items'
        parentFolder values, we extract the real root folder ID.
        """
        logger.info("Discovering applicationData root folder ID...")

        # List root-level items using 'applicationData' as keyword
        result = client.list_files(parent_folder="applicationData", page_size=10)
        files = result.get("files", [])

        if files:
            # All root-level items share the same parentFolder = real root ID
            parents: set[str] = set()
            for f in files:
                pf = f.get("parentFolder", [])
                for p in pf:
                    pid = p if isinstance(p, str) else p.get("id", "")
                    if pid:
                        parents.add(pid)
            if parents:
                real_id = parents.pop()
                logger.info("applicationData root → %s (%d root items)", real_id, len(files))
                return real_id

        # Empty container — can't discover root ID
        logger.warning("applicationData container appears empty")
        return "applicationData"
