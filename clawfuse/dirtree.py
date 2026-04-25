"""Directory tree — metadata-only tree loaded from Drive Kit.

Loads all file/folder metadata at startup (no content), builds an in-memory
path tree for fast path→FileMeta resolution.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

from .client import DriveKitClient
from .config import FOLDER_MIME

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
    """In-memory directory tree built from Drive Kit metadata."""

    def __init__(
        self,
        client: DriveKitClient,
        root_folder: str = "applicationData",
        refresh_ttl: float = 10.0,
    ) -> None:
        self._client = client
        self._root_folder = root_folder
        self._refresh_ttl = refresh_ttl
        self._last_refresh: float = 0.0

        # path → FileMeta (e.g. "/data/report.csv" → FileMeta)
        self._path_map: dict[str, FileMeta] = {}
        # file_id → path
        self._id_map: dict[str, str] = {}
        # parent_id → list of child FileMeta (for fast list_dir)
        self._children_map: dict[str, list[FileMeta]] = {}

    # ── Public API ──

    def refresh(self) -> None:
        """Load all file metadata from Drive Kit and rebuild the tree."""
        start = time.monotonic()
        all_items = self._client.list_all_files(root_folder=self._root_folder)
        self._build_tree(all_items)
        self._last_refresh = time.monotonic()
        elapsed = self._last_refresh - start
        logger.info("DirTree refreshed: %d items in %.2fs", len(self._path_map), elapsed)

    def resolve(self, path: str) -> FileMeta | None:
        """Resolve a path to FileMeta. Auto-refreshes if TTL expired."""
        if self._should_refresh():
            self.refresh()
        return self._path_map.get(self._normalize(path))

    def list_dir(self, path: str) -> list[str]:
        """List direct children of a directory. Uses parent_id index for O(1) lookup."""
        if self._should_refresh():
            self.refresh()

        normalized = self._normalize(path)
        # Find the file_id for this directory path
        meta = self._path_map.get(normalized)
        if normalized == "/":
            parent_id = self._root_folder
        elif meta is not None:
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
        normalized = self._normalize(path)
        self._path_map[normalized] = meta
        self._id_map[meta.id] = normalized
        if meta.parent_id not in self._children_map:
            self._children_map[meta.parent_id] = []
        self._children_map[meta.parent_id].append(meta)

    def remove_entry(self, path: str) -> None:
        """Remove an entry by path."""
        normalized = self._normalize(path)
        meta = self._path_map.pop(normalized, None)
        if meta:
            self._id_map.pop(meta.id, None)
            children = self._children_map.get(meta.parent_id, [])
            self._children_map[meta.parent_id] = [c for c in children if c.id != meta.id]

    def move_entry(self, old_path: str, new_path: str) -> None:
        """Move/rename an entry."""
        old_norm = self._normalize(old_path)
        new_norm = self._normalize(new_path)
        meta = self._path_map.pop(old_norm, None)
        if meta:
            # Remove from old parent's children
            old_children = self._children_map.get(meta.parent_id, [])
            self._children_map[meta.parent_id] = [c for c in old_children if c.id != meta.id]

            # Update parent_id based on new_path
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
            # Add to new parent's children
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

    # ── Internal ──

    def _build_tree(self, raw_items: list[dict]) -> None:
        """Build path maps from raw Drive Kit items."""
        # Reset
        self._path_map.clear()
        self._id_map.clear()
        self._children_map.clear()

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
            path = self._resolve_path(item_id, id_to_raw, path_cache)
            if path is None:
                continue

            mime = raw.get("mimeType", "")
            is_dir = mime == FOLDER_MIME
            parents = raw.get("parentFolder", [])
            parent_id = parents[0]["id"] if parents else ""

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
            # Index by parent for fast list_dir
            if parent_id not in self._children_map:
                self._children_map[parent_id] = []
            self._children_map[parent_id].append(meta)

    def _resolve_path(
        self,
        item_id: str,
        id_to_raw: dict[str, dict],
        cache: dict[str, str],
    ) -> str | None:
        """Recursively resolve a file_id to a full path via parent chain."""
        if item_id in cache:
            return cache[item_id]

        raw = id_to_raw.get(item_id)
        if not raw:
            return None

        # Root folder itself — skip
        if item_id == self._root_folder:
            cache[item_id] = "/"
            return "/"

        name = raw.get("fileName", "")
        if not name:
            return None

        # Skip hidden files
        if name.startswith("."):
            cache[item_id] = ""  # Mark as visited but invalid
            return None

        parents = raw.get("parentFolder", [])
        parent_id = parents[0]["id"] if parents else ""

        if not parent_id or parent_id == self._root_folder:
            path = "/" + name
        else:
            parent_path = self._resolve_path(parent_id, id_to_raw, cache)
            if parent_path is None or parent_path == "":
                return None
            path = parent_path + "/" + name

        cache[item_id] = path
        return path

    def _normalize(self, path: str) -> str:
        """Normalize path: remove trailing slash, ensure leading slash."""
        if not path or path == "/":
            return "/"
        # Remove trailing slash
        path = path.rstrip("/")
        # Ensure leading slash
        if not path.startswith("/"):
            path = "/" + path
        return path

    def _should_refresh(self) -> bool:
        """Check if TTL has expired."""
        if self._last_refresh == 0:
            return True
        return (time.monotonic() - self._last_refresh) > self._refresh_ttl
