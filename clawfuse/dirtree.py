"""Directory tree — lazy-loaded metadata tree backed by Drive Kit.

Supports two modes:
1. Legacy: refresh() loads all metadata via BFS at once (blocking).
2. Lazy: load_dir() loads individual directories on demand, with
   background_full_load() running BFS in a daemon thread.

The lazy mode is the default for new code. Both modes share the same
in-memory data structures (_path_map, _children_map, _id_map).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .config import FOLDER_MIME

if TYPE_CHECKING:
    from .client import DriveKitClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileMeta:
    """Metadata for a file or folder."""

    id: str
    name: str
    is_dir: bool
    size: int
    sha256: str
    parent_id: str
    modified_time: str


class DirTree:
    """In-memory directory tree loaded from Drive Kit metadata.

    Supports lazy per-directory loading and background BFS preloading.
    Thread-safe: multiple threads can call load_dir / ensure_loaded concurrently.
    """

    def __init__(
        self,
        client: DriveKitClient,
        root_folder: str = "applicationData",
        refresh_ttl: float = 10.0,
        load_wait_timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._root_folder = root_folder
        self._refresh_ttl = refresh_ttl
        self._load_wait_timeout = load_wait_timeout
        self._last_refresh: float = 0.0

        # Core indexes (shared by both legacy and lazy modes)
        self._path_map: dict[str, FileMeta] = {}
        self._id_map: dict[str, str] = {}
        self._children_map: dict[str, list[FileMeta]] = {}

        # Lazy-loading state
        self._loaded_dirs: set[str] = set()     # dir_ids already loaded from API
        self._loading: set[str] = set()          # dir_ids currently being loaded
        self._failed_dirs: dict[str, float] = {}  # dir_id → timestamp of last failure (circuit breaker)
        self._lock = threading.Lock()            # protects _loaded_dirs, _loading, and indexes
        self._load_condition = threading.Condition(self._lock)  # wait/notify for load completion

        # Background loading state
        self._bg_complete = False
        self._lazy_mode: bool = False  # True when using load_dir/ensure_loaded

    # ── Public API ──

    def refresh(self) -> None:
        """Legacy: load all file metadata from Drive Kit and rebuild the tree.

        Blocks until complete. For large trees, prefer lazy mode.
        """
        start = time.monotonic()
        all_items = self._client.list_all_files(root_folder=self._root_folder)
        self._build_tree(all_items)
        self._last_refresh = time.monotonic()
        elapsed = self._last_refresh - start
        logger.info("DirTree refreshed: %d items in %.2fs", len(self._path_map), elapsed)

    def load_dir(self, dir_id: str) -> None:
        """Load a single directory's direct children from Drive Kit API.

        Thread-safe. If the directory is already loaded, returns immediately.
        If another thread is currently loading this directory, blocks until
        it finishes (with timeout to prevent indefinite blocking).
        Circuit breaker: if the directory recently failed, skip loading for
        a cooldown period to avoid hammering a failing API.
        """
        self._lazy_mode = True

        # Fast path: already loaded (no lock needed due to GIL for set membership)
        if dir_id in self._loaded_dirs:
            return

        # Circuit breaker: skip if this dir recently failed (within cooldown)
        with self._lock:
            fail_time = self._failed_dirs.get(dir_id)
            if fail_time is not None and (time.monotonic() - fail_time) < 5.0:
                logger.debug("Skipping load for recently failed dir %s (cooldown)", dir_id)
                return

        with self._load_condition:
            if dir_id in self._loaded_dirs:  # double-check after acquiring lock
                return
            if dir_id in self._loading:
                # Another thread is loading this dir — wait with timeout
                deadline = time.monotonic() + self._load_wait_timeout
                while dir_id in self._loading:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning("Timeout waiting for dir %s to load", dir_id)
                        return  # Don't block FUSE indefinitely
                    self._load_condition.wait(timeout=remaining)
                return
            self._loading.add(dir_id)

        try:
            self._load_dir_from_api(dir_id)
            # Success — clear any previous failure record
            with self._lock:
                self._failed_dirs.pop(dir_id, None)
        except Exception:
            # Record failure time for circuit breaker cooldown
            with self._lock:
                self._failed_dirs[dir_id] = time.monotonic()
            raise
        finally:
            with self._load_condition:
                self._loading.discard(dir_id)
                self._load_condition.notify_all()

    def ensure_loaded(self, dir_path: str) -> None:
        """Ensure dir_path and all ancestor directories are loaded.

        Walks from root to dir_path, calling load_dir for each unloaded level.
        Called by FUSE getattr/readdir before accessing the tree.
        """
        if dir_path == "/" or dir_path == "":
            # Ensure root is loaded
            self.load_dir(self._root_folder)
            return

        parts = PurePosixPath(dir_path).parts  # ("/", "data", "reports", "2026")
        current_path = "/"
        current_id = self._root_folder

        for part in parts[1:]:  # skip "/"
            child_path = current_path.rstrip("/") + "/" + part

            # Ensure the current level is loaded so we can find child's id
            self.load_dir(current_id)

            # Look up the child directory's id
            child_meta = self._path_map.get(child_path)
            if child_meta is None:
                break  # Path doesn't exist — getattr will report ENOENT

            current_path = child_path
            current_id = child_meta.id

        # Load the target directory itself
        self.load_dir(current_id)

    def background_full_load(self, max_workers: int = 8) -> None:
        """Background BFS: load all directory metadata as fast as possible.

        Uses ThreadPoolExecutor for parallel directory loading.
        Drive Kit API handles 8 concurrent requests well (QPS ~4.6).

        Starts from root, BFS through the tree. Skips directories already
        loaded (by user requests or previous runs). Runs until complete.
        """
        loaded_count = 0
        next_batch = [self._root_folder]

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            while next_batch:
                # Filter out already-loaded dirs before submitting
                to_load = []
                already_loaded_subdirs: list[str] = []

                for dir_id in next_batch:
                    if dir_id in self._loaded_dirs:
                        # Already loaded by user request — collect its sub-folders
                        with self._lock:
                            children = list(self._children_map.get(dir_id, []))
                        already_loaded_subdirs.extend(
                            c.id for c in children if c.is_dir
                        )
                    else:
                        to_load.append(dir_id)

                # Submit loading tasks
                future_to_id = {
                    pool.submit(self.load_dir, dir_id): dir_id
                    for dir_id in to_load
                }

                # Collect sub-folders from loaded dirs
                new_subdirs: list[str] = list(already_loaded_subdirs)

                for future in as_completed(future_to_id):
                    dir_id = future_to_id[future]
                    try:
                        future.result()  # propagate exceptions
                        loaded_count += 1
                    except Exception as e:
                        logger.warning("Failed to load dir %s: %s", dir_id, e)

                    # Collect sub-folders regardless of success/failure
                    with self._lock:
                        children = list(self._children_map.get(dir_id, []))
                    new_subdirs.extend(c.id for c in children if c.is_dir)

                next_batch = new_subdirs

        self._bg_complete = True
        logger.info(
            "Background full load complete: %d dirs loaded, %d total items",
            loaded_count,
            len(self._path_map),
        )

    def resolve(self, path: str) -> FileMeta | None:
        """Resolve a path to FileMeta. Auto-refreshes if TTL expired (legacy)."""
        if self._should_refresh():
            self.refresh()
        return self._path_map.get(self._normalize(path))

    def list_dir(self, path: str) -> list[str]:
        """List direct children of a directory."""
        if self._should_refresh():
            self.refresh()

        normalized = self._normalize(path)
        if normalized == "/":
            parent_id = self._root_folder
        else:
            meta = self._path_map.get(normalized)
            if meta is not None:
                parent_id = meta.id
            else:
                return []

        children = self._children_map.get(parent_id, [])
        return sorted(m.name for m in children)

    def get_path(self, file_id: str) -> str | None:
        """Reverse lookup: file_id → path."""
        return self._id_map.get(file_id)

    def add_entry(self, path: str, meta: FileMeta) -> None:
        """Add a new file/folder entry."""
        with self._lock:
            normalized = self._normalize(path)
            self._path_map[normalized] = meta
            self._id_map[meta.id] = normalized
            if meta.parent_id not in self._children_map:
                self._children_map[meta.parent_id] = []
            self._children_map[meta.parent_id].append(meta)

    def update_meta(self, path: str, **fields: object) -> None:
        """Replace a FileMeta entry, updating only the given fields.

        Uses dataclasses.replace() to create a new frozen instance.
        """
        from dataclasses import replace

        with self._lock:
            normalized = self._normalize(path)
            meta = self._path_map.get(normalized)
            if meta is None:
                return
            new_meta = replace(meta, **fields)
            self._path_map[normalized] = new_meta
            # Update children_map reference (same id, so just replace in list)
            children = self._children_map.get(meta.parent_id, [])
            for i, c in enumerate(children):
                if c.id == meta.id:
                    children[i] = new_meta
                    break

    def remove_entry(self, path: str) -> None:
        """Remove an entry by path."""
        with self._lock:
            normalized = self._normalize(path)
            meta = self._path_map.pop(normalized, None)
            if meta:
                self._id_map.pop(meta.id, None)
                children = self._children_map.get(meta.parent_id, [])
                self._children_map[meta.parent_id] = [c for c in children if c.id != meta.id]

    def move_entry(self, old_path: str, new_path: str) -> None:
        """Move/rename an entry."""
        with self._lock:
            old_norm = self._normalize(old_path)
            new_norm = self._normalize(new_path)
            meta = self._path_map.pop(old_norm, None)
            if meta:
                old_children = self._children_map.get(meta.parent_id, [])
                self._children_map[meta.parent_id] = [c for c in old_children if c.id != meta.id]

                new_parent_path = str(PurePosixPath(new_norm).parent)
                new_parent_id = ""
                parent_meta = self._path_map.get(new_parent_path)
                if parent_meta:
                    new_parent_id = parent_meta.id
                elif new_parent_path == "/":
                    new_parent_id = self._root_folder

                new_meta = FileMeta(
                    id=meta.id,
                    name=PurePosixPath(new_norm).name,
                    is_dir=meta.is_dir,
                    size=meta.size,
                    sha256=meta.sha256,
                    parent_id=new_parent_id,
                    modified_time=meta.modified_time,
                )
                self._path_map[new_norm] = new_meta
                self._id_map[meta.id] = new_norm
                if new_parent_id not in self._children_map:
                    self._children_map[new_parent_id] = []
                self._children_map[new_parent_id].append(new_meta)
            else:
                logger.warning("move_entry: old path not found: %s", old_path)

    @property
    def file_count(self) -> int:
        """Total files + folders in tree."""
        return len(self._path_map)

    @property
    def last_refresh_time(self) -> float:
        return self._last_refresh

    @property
    def bg_complete(self) -> bool:
        """Whether background full load has finished."""
        return self._bg_complete

    @property
    def loaded_dir_count(self) -> int:
        """Number of directories loaded from API so far."""
        return len(self._loaded_dirs)

    # ── Internal ──

    def _load_dir_from_api(self, dir_id: str) -> None:
        """Call list_files API for dir_id, merge results into indexes.

        Uses queryParam='{dir_id}' in parentFolder to list only direct
        children of the requested directory. Paginates through all pages
        using the same while-True pattern as list_all_files / _resolve_root_folder.
        """
        all_files: list[dict] = []
        cursor: str | None = None
        page_count = 0

        while True:
            result = self._client.list_files(parent_folder=dir_id, cursor=cursor)
            files = result.get("files", [])
            all_files.extend(files)
            page_count += 1

            cursor = result.get("nextCursor")
            if not cursor or not files:
                break

        if page_count > 1:
            logger.info(
                "Paginated dir %s: %d items across %d pages",
                dir_id, len(all_files), page_count,
            )

        # Build path cache for resolving parent chains
        path_cache: dict[str, str] = {}

        with self._lock:
            for item in all_files:
                item_id = item.get("id", "")
                if not item_id:
                    continue

                mime = item.get("mimeType", "")
                is_dir = mime == FOLDER_MIME
                name = item.get("fileName", "")

                # Skip hidden files
                if name.startswith("."):
                    continue
                if not name:
                    continue

                parents = item.get("parentFolder", [])
                parent_id = (parents[0]["id"] if isinstance(parents[0], dict) else parents[0]) if parents else ""

                # Resolve full path
                path = self._resolve_path_for(item_id, parent_id, name, path_cache)
                if path is None:
                    continue

                meta = FileMeta(
                    id=item_id,
                    name=name,
                    is_dir=is_dir,
                    size=int(item.get("size", 0)),
                    sha256=item.get("sha256", "") or "",
                    parent_id=parent_id,
                    modified_time=item.get("modifiedTime", ""),
                )
                self._path_map[path] = meta
                self._id_map[item_id] = path
                if parent_id not in self._children_map:
                    self._children_map[parent_id] = []
                self._children_map[parent_id].append(meta)

            # Mark the requested directory as loaded
            self._loaded_dirs.add(dir_id)

    def _resolve_path_for(
        self,
        item_id: str,
        parent_id: str,
        name: str,
        cache: dict[str, str],
    ) -> str | None:
        """Resolve an item's full path using parent chain and cache."""
        if item_id in cache:
            return cache[item_id]

        # Root folder itself — skip
        if item_id == self._root_folder:
            cache[item_id] = "/"
            return "/"

        if not parent_id or parent_id == self._root_folder:
            path = "/" + name
        else:
            parent_path = self._resolve_path_cached(parent_id, cache)
            if parent_path is None or parent_path == "":
                return None
            path = parent_path + "/" + name

        cache[item_id] = path
        return path

    def _resolve_path_cached(self, parent_id: str, cache: dict[str, str]) -> str | None:
        """Resolve parent path using cache + existing path_map."""
        if parent_id in cache:
            return cache[parent_id]

        # Check if we already have this parent in path_map
        existing = self._id_map.get(parent_id)
        if existing is not None:
            cache[parent_id] = existing
            return existing

        # Parent not loaded yet — this is normal in lazy mode
        # We'll still add the child; its path is relative to what we know
        return None

    def _build_tree(self, raw_items: list[dict]) -> None:
        """Build path maps from raw Drive Kit items (legacy refresh mode)."""
        with self._lock:
            self._path_map.clear()
            self._id_map.clear()
            self._children_map.clear()
            self._loaded_dirs.clear()

            # Index by id for parent lookup
            id_to_raw: dict[str, dict] = {}
            for item in raw_items:
                item_id = item.get("id", "")
                if not item_id:
                    continue
                id_to_raw[item_id] = item

            # Build paths from parent chain
            path_cache: dict[str, str] = {}

            for item_id, raw in id_to_raw.items():
                path = self._resolve_path_legacy(item_id, id_to_raw, path_cache)
                if path is None:
                    continue

                mime = raw.get("mimeType", "")
                is_dir = mime == FOLDER_MIME
                parents = raw.get("parentFolder", [])
                parent_id = (parents[0]["id"] if isinstance(parents[0], dict) else parents[0]) if parents else ""

                meta = FileMeta(
                    id=item_id,
                    name=raw.get("fileName", ""),
                    is_dir=is_dir,
                    size=int(raw.get("size", 0)),
                    sha256=raw.get("sha256", "") or "",
                    parent_id=parent_id,
                    modified_time=raw.get("modifiedTime", ""),
                )
                self._path_map[path] = meta
                self._id_map[item_id] = path
                if parent_id not in self._children_map:
                    self._children_map[parent_id] = []
                self._children_map[parent_id].append(meta)

            # In legacy mode, all directories are fully loaded via list_all_files.
            # Mark every directory (including root) so lazy-mode code skips them.
            self._loaded_dirs.add(self._root_folder)
            for path, meta in self._path_map.items():
                if meta.is_dir:
                    self._loaded_dirs.add(meta.id)

    def _resolve_path_legacy(
        self,
        item_id: str,
        id_to_raw: dict[str, dict],
        cache: dict[str, str],
    ) -> str | None:
        """Recursively resolve a file_id to a full path via parent chain (legacy)."""
        if item_id in cache:
            return cache[item_id]

        raw = id_to_raw.get(item_id)
        if not raw:
            return None

        if item_id == self._root_folder:
            cache[item_id] = "/"
            return "/"

        name = raw.get("fileName", "")
        if not name:
            return None

        if name.startswith("."):
            cache[item_id] = ""
            return None

        parents = raw.get("parentFolder", [])
        parent_id = (parents[0]["id"] if isinstance(parents[0], dict) else parents[0]) if parents else ""

        if not parent_id or parent_id == self._root_folder:
            path = "/" + name
        else:
            parent_path = self._resolve_path_legacy(parent_id, id_to_raw, cache)
            if parent_path is None or parent_path == "":
                return None
            path = parent_path + "/" + name

        cache[item_id] = path
        return path

    def _normalize(self, path: str) -> str:
        """Normalize path: remove trailing slash, ensure leading slash."""
        if not path or path == "/":
            return "/"
        path = path.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return path

    def _should_refresh(self) -> bool:
        """Check if TTL has expired (legacy mode only).

        Returns False when lazy mode is active — load_dir/ensure_loaded
        manage their own freshness and should never trigger a full refresh
        that would wipe lazy-loaded data.
        """
        if self._lazy_mode:
            return False
        if self._last_refresh == 0:
            return True
        return (time.monotonic() - self._last_refresh) > self._refresh_ttl
