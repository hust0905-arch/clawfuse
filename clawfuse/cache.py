"""Disk-based LRU content cache for file data.

Stores downloaded file content on disk with .content + .meta sidecar files.
Uses OrderedDict for LRU eviction when cache exceeds size limits.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from .exceptions import CacheError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheEntry:
    """Metadata for a cached file."""

    file_id: str
    path: str
    size: int
    sha256: str
    last_access: float
    disk_path: Path


class ContentCache:
    """Disk-based LRU content cache."""

    def __init__(self, cache_dir: Path, max_bytes: int, max_files: int) -> None:
        self._cache_dir = cache_dir
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._total_bytes: int = 0

        # LRU index: file_id → CacheEntry, ordered by access time
        self._lru: OrderedDict[str, CacheEntry] = OrderedDict()

        # Create cache directory
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        # Restore from disk
        self._restore_from_disk()

    def get(self, file_id: str) -> bytes | None:
        """Get cached content. Returns None on cache miss."""
        entry = self._lru.get(file_id)
        if entry is None:
            return None

        # Read from disk
        try:
            content = entry.disk_path.read_bytes()
        except FileNotFoundError:
            # Cache file was deleted externally
            self._remove_entry(file_id)
            return None
        except OSError as e:
            logger.warning("Cache read error for %s: %s", file_id, e)
            self._remove_entry(file_id)
            return None

        # Update LRU order (move to end = most recently used)
        self._lru.move_to_end(file_id)
        return content

    def put(self, file_id: str, path: str, content: bytes, sha256: str) -> None:
        """Store content in cache. Evicts LRU entries if needed."""
        # Remove existing entry if present
        if file_id in self._lru:
            self._remove_entry(file_id)

        # Write content to disk (atomic)
        disk_path = self._disk_path(file_id)
        self._write_atomic(disk_path, content)

        # Write .meta sidecar
        meta = {
            "file_id": file_id,
            "path": path,
            "size": len(content),
            "sha256": sha256,
            "last_access": __import__("time").time(),
        }
        meta_path = disk_path.with_suffix(".meta")
        self._write_atomic(meta_path, json.dumps(meta, indent=2).encode())

        # Add to LRU index
        import time

        entry = CacheEntry(
            file_id=file_id,
            path=path,
            size=len(content),
            sha256=sha256,
            last_access=time.time(),
            disk_path=disk_path,
        )
        self._lru[file_id] = entry
        self._total_bytes += entry.size

        # Evict if over budget
        self._evict_if_needed()

    def invalidate(self, file_id: str) -> None:
        """Remove a cache entry."""
        self._remove_entry(file_id)

    def contains(self, file_id: str) -> bool:
        """Check if file is in cache index."""
        return file_id in self._lru

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def entry_count(self) -> int:
        return len(self._lru)

    # ── Internal ──

    def _disk_path(self, file_id: str) -> Path:
        """Get disk path for a file_id. Uses first 2 chars as subdirectory."""
        prefix = file_id[:2] if len(file_id) >= 2 else "__"
        subdir = self._cache_dir / prefix
        subdir.mkdir(exist_ok=True)
        return subdir / f"{file_id}.content"

    def _write_atomic(self, path: Path, data: bytes) -> None:
        """Write data atomically: write to .tmp then rename."""
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_bytes(data)
            tmp_path.rename(path)
        except OSError as e:
            # Clean up tmp file on failure
            tmp_path.unlink(missing_ok=True)
            raise CacheError(f"Failed to write {path}: {e}") from e

    def _remove_entry(self, file_id: str) -> None:
        """Remove entry from index and disk."""
        entry = self._lru.pop(file_id, None)
        if entry is None:
            return
        self._total_bytes -= entry.size
        # Delete disk files
        entry.disk_path.unlink(missing_ok=True)
        entry.disk_path.with_suffix(".meta").unlink(missing_ok=True)

    def _evict_if_needed(self) -> None:
        """Evict LRU entries until within budget."""
        while self._total_bytes > self._max_bytes and self._lru:
            file_id, entry = self._lru.popitem(last=False)  # Pop oldest
            self._total_bytes -= entry.size
            entry.disk_path.unlink(missing_ok=True)
            entry.disk_path.with_suffix(".meta").unlink(missing_ok=True)
            logger.debug("Cache evicted: %s (%d bytes)", file_id, entry.size)

        # Also enforce max_files
        while len(self._lru) > self._max_files:
            file_id, entry = self._lru.popitem(last=False)
            self._total_bytes -= entry.size
            entry.disk_path.unlink(missing_ok=True)
            entry.disk_path.with_suffix(".meta").unlink(missing_ok=True)
            logger.debug("Cache evicted (max_files): %s", file_id)

    def _restore_from_disk(self) -> None:
        """Scan cache_dir for .meta files and rebuild LRU index."""
        import time

        meta_files = list(self._cache_dir.rglob("*.meta"))
        if not meta_files:
            return

        restored = 0
        for meta_path in meta_files:
            try:
                raw = meta_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                file_id = data["file_id"]

                content_path = meta_path.with_suffix(".content")
                if not content_path.exists():
                    meta_path.unlink(missing_ok=True)
                    continue

                size = data.get("size", content_path.stat().st_size)
                entry = CacheEntry(
                    file_id=file_id,
                    path=data.get("path", ""),
                    size=size,
                    sha256=data.get("sha256", ""),
                    last_access=data.get("last_access", 0),
                    disk_path=content_path,
                )
                self._lru[file_id] = entry
                self._total_bytes += size
                restored += 1
            except (json.JSONDecodeError, OSError, KeyError) as e:
                logger.warning("Skipping corrupt cache meta %s: %s", meta_path, e)

        if restored:
            logger.info("Restored %d cache entries (%d bytes)", restored, self._total_bytes)
