"""FUSE filesystem operations for ClawFUSE.

Implements fusepy Operations interface to expose Drive Kit cloud storage
as a local POSIX filesystem.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
import time
from pathlib import PurePosixPath

from .cache import ContentCache
from .client import DriveKitClient
from .config import FOLDER_MIME
from .dirtree import DirTree, FileMeta
from .writebuf import WriteBuffer

logger = logging.getLogger(__name__)

# UID/GID for all files (container's user)
_DEFAULT_UID = os.getuid() if hasattr(os, "getuid") else 0
_DEFAULT_GID = os.getgid() if hasattr(os, "getgid") else 0

# Import Operations base class (fusepy). Not installed on Windows dev machines,
# but always available in the container environment.
try:
    from fuse import Operations as _FuseOperations
except ImportError:
    _FuseOperations = object  # type: ignore[misc,assignment]


class ClawFUSE(_FuseOperations):  # type: ignore[misc]
    """FUSE filesystem operations backed by Drive Kit."""

    def __init__(
        self,
        client: DriveKitClient,
        dirtree: DirTree,
        cache: ContentCache,
        writebuf: WriteBuffer,
        root_folder: str = "applicationData",
    ) -> None:
        self._client = client
        self._dirtree = dirtree
        self._cache = cache
        self._writebuf = writebuf
        self._root_folder = root_folder

        # File handle state
        self._fh_map: dict[int, str] = {}  # fh → file_id
        self._content_map: dict[int, bytearray] = {}  # fh → write buffer
        self._dirty: set[int] = set()
        self._next_fh: int = 1

    # ── File operations ──

    def getattr(self, path: str, fh: int | None = None) -> dict:
        """Get file/directory attributes."""
        if path == "/":
            return self._dir_stat()

        # Ensure parent directory is loaded before resolve.
        # This must happen BEFORE the first resolve() call to prevent
        # resolve() from triggering a legacy refresh() that would
        # mark directories as loaded without actually fetching them.
        parent = str(PurePosixPath(path).parent)
        self._dirtree.ensure_loaded(parent)

        meta = self._dirtree.resolve(path)
        if meta is None:
            self._raise(errno.ENOENT, path)

        if meta.is_dir:
            return self._dir_stat()
        return self._file_stat(meta.size)

    def readdir(self, path: str, fh: int) -> list[str]:
        """List directory contents."""
        self._dirtree.ensure_loaded(path)
        entries = self._dirtree.list_dir(path)
        return [".", ".."] + entries

    def open(self, path: str, flags: int) -> int:
        """Open a file."""
        meta = self._dirtree.resolve(path)
        if meta is None:
            self._raise(errno.ENOENT, path)

        fh = self._alloc_fh()
        self._fh_map[fh] = meta.id

        # For write modes, initialize content buffer with existing content
        if flags & (os.O_WRONLY | os.O_RDWR):
            existing = self._cache.get(meta.id)
            if existing is None:
                # Cache miss — download from cloud to avoid data loss
                existing = self._client.download_file(meta.id)
            self._content_map[fh] = bytearray(existing)

        return fh

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        """Read file content."""
        file_id = self._fh_map.get(fh)
        if file_id is None:
            self._raise(errno.EBADF, path)

        # If writing, read from in-memory buffer
        if fh in self._content_map:
            content = self._content_map[fh]
            return bytes(content[offset : offset + size])

        # Try cache
        content = self._cache.get(file_id)
        if content is None:
            # Download from Drive Kit
            try:
                content = self._client.download_file(file_id)
            except Exception as e:
                logger.error("Download failed for %s: %s", path, e)
                self._raise(errno.EIO, path)

            # Get sha256 for cache
            sha256 = hashlib.sha256(content).hexdigest()
            self._cache.put(file_id, path, content, sha256)

        return content[offset : offset + size]

    def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
        """Write data to file.

        Uses bytearray with slice assignment to avoid O(n^2) copies.
        Each write extends in-place instead of copying the entire buffer.
        """
        if fh not in self._content_map:
            # Initialize buffer with existing content if first write
            file_id = self._fh_map.get(fh, "")
            existing = self._cache.get(file_id)
            if existing is None:
                existing = self._client.download_file(file_id)
            self._content_map[fh] = bytearray(existing)

        buf = self._content_map[fh]
        # Extend buffer if needed
        end = offset + len(data)
        if end > len(buf):
            buf.extend(b"\x00" * (end - len(buf)))
        buf[offset : offset + len(data)] = data
        self._dirty.add(fh)

        return len(data)

    def create(self, path: str, mode: int) -> int:
        """Create a new file."""
        parent_path = str(PurePosixPath(path).parent)
        filename = PurePosixPath(path).name

        # Resolve parent folder (root uses root_folder ID)
        parent_id = self._root_folder
        if parent_path != "/":
            parent_meta = self._dirtree.resolve(parent_path)
            if parent_meta is None or not parent_meta.is_dir:
                self._raise(errno.ENOENT)
            parent_id = parent_meta.id

        # Create empty file on Drive Kit
        result = self._client.create_file(
            filename=filename,
            content=b"",
            parent_folder=parent_id,
        )

        file_id = result.get("id", "")
        sha256 = result.get("sha256", "")

        meta = FileMeta(
            id=file_id,
            name=filename,
            is_dir=False,
            size=0,
            sha256=sha256,
            parent_id=parent_id,
            modified_time=result.get("modifiedTime", ""),
        )
        self._dirtree.add_entry(path, meta)

        fh = self._alloc_fh()
        self._fh_map[fh] = file_id
        self._content_map[fh] = bytearray()
        return fh

    def flush(self, path: str, fh: int) -> None:
        """Flush file: enqueue dirty content to write buffer and update cache."""
        if fh in self._dirty:
            content = self._content_map.get(fh, b"")
            # Convert bytearray to bytes for cache and write buffer
            if isinstance(content, bytearray):
                content = bytes(content)
            file_id = self._fh_map.get(fh, "")
            sha256 = hashlib.sha256(content).hexdigest()
            self._writebuf.enqueue(file_id, path, content, sha256)
            # Also update cache so subsequent reads see the latest content
            self._cache.put(file_id, path, content, sha256)
            # Update size in dirtree so getattr returns correct size
            if path:
                self._dirtree.update_meta(path, size=len(content), sha256=sha256)
            self._dirty.discard(fh)

    def release(self, path: str, fh: int) -> None:
        """Close file: flush if dirty, clean up state."""
        if fh in self._dirty:
            self.flush(path, fh)

        self._fh_map.pop(fh, None)
        self._content_map.pop(fh, None)
        self._dirty.discard(fh)

    def unlink(self, path: str) -> None:
        """Delete a file."""
        meta = self._dirtree.resolve(path)
        if meta is None:
            self._raise(errno.ENOENT, path)

        self._client.delete_file(meta.id)
        self._cache.invalidate(meta.id)
        self._dirtree.remove_entry(path)

    def truncate(self, path: str, length: int, fh: int | None = None) -> None:
        """Truncate file to specified length."""
        if fh is not None and fh in self._content_map:
            buf = self._content_map[fh]
            if isinstance(buf, bytes):
                buf = bytearray(buf)
                self._content_map[fh] = buf
            if length < len(buf):
                del buf[length:]
            else:
                buf.extend(b"\x00" * (length - len(buf)))
            self._dirty.add(fh)
            return

        # No open fh — download, truncate, upload
        meta = self._dirtree.resolve(path)
        if meta is None:
            self._raise(errno.ENOENT, path)

        content = self._cache.get(meta.id) or self._client.download_file(meta.id)
        if length < len(content):
            content = content[:length]
        else:
            content = content + b"\x00" * (length - len(content))

        sha256 = hashlib.sha256(content).hexdigest()
        self._writebuf.enqueue(meta.id, path, content, sha256)
        self._dirtree.update_meta(path, size=len(content), sha256=sha256)

    # ── Directory operations ──

    def mkdir(self, path: str, mode: int) -> None:
        """Create a directory."""
        parent_path = str(PurePosixPath(path).parent)
        folder_name = PurePosixPath(path).name

        parent_id = self._root_folder
        if parent_path != "/":
            parent_meta = self._dirtree.resolve(parent_path)
            if parent_meta is None or not parent_meta.is_dir:
                self._raise(errno.ENOENT)
            parent_id = parent_meta.id

        result = self._client.create_folder(folder_name, parent_id)
        folder_id = result.get("id", "")

        meta = FileMeta(
            id=folder_id,
            name=folder_name,
            is_dir=True,
            size=0,
            sha256="",
            parent_id=parent_id,
            modified_time="",
        )
        self._dirtree.add_entry(path, meta)

    def rmdir(self, path: str) -> None:
        """Remove an empty directory."""
        meta = self._dirtree.resolve(path)
        if meta is None or not meta.is_dir:
            self._raise(errno.ENOENT, path)

        # Check if empty
        children = self._dirtree.list_dir(path)
        if children:
            self._raise(errno.ENOTEMPTY, path)

        self._client.delete_file(meta.id)
        self._dirtree.remove_entry(path)

    def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move a file or directory."""
        meta = self._dirtree.resolve(old_path)
        if meta is None:
            self._raise(errno.ENOENT, old_path)

        new_parent_path = str(PurePosixPath(new_path).parent)
        new_name = PurePosixPath(new_path).name

        new_parent_id = self._root_folder
        if new_parent_path != "/":
            new_parent_meta = self._dirtree.resolve(new_parent_path)
            if new_parent_meta is None or not new_parent_meta.is_dir:
                self._raise(errno.ENOENT)
            new_parent_id = new_parent_meta.id

        self._client.update_metadata(
            meta.id,
            fileName=new_name,
            parentFolder=[new_parent_id],
        )
        self._dirtree.move_entry(old_path, new_path)

    # ── No-op operations ──

    def chmod(self, path: str, mode: int) -> None:
        pass

    def chown(self, path: str, uid: int, gid: int) -> None:
        pass

    def utimens(self, path: str, times: tuple | None = None) -> None:
        pass

    def access(self, path: str, amode: int) -> None:
        pass

    def statfs(self, path: str) -> dict:
        return {
            "f_bsize": 4096,
            "f_blocks": 1024 * 1024,
            "f_bavail": 512 * 1024,
            "f_bfree": 512 * 1024,
            "f_files": 10000,
        }

    # ── Lifecycle ──

    def destroy(self, private_data: int) -> None:
        """FUSE unmount callback. Flush all dirty data."""
        logger.info("FUSE destroy called — flushing dirty data")
        for fh in list(self._dirty):
            if fh in self._fh_map:
                self.flush("", fh)

    def mount(self, mountpoint: str, foreground: bool = False) -> None:
        """Mount the FUSE filesystem.

        Always uses foreground=True internally. fusepy's own daemonization
        (foreground=False) calls os.fork() which kills all background threads
        (BFS loader, writebuf drain), leaving _loading set poisoned and causing
        deadlock on the first readdir. Background mode should be achieved via
        nohup/systemd instead.
        """
        try:
            from fuse import FUSE
        except ImportError:
            from .exceptions import MountError

            raise MountError("fusepy not installed. Run: pip install clawfuse[fuse]")

        logger.info("Mounting ClawFUSE at %s", mountpoint)
        FUSE(self, mountpoint, foreground=True, ro=False, allow_other=True)

    # ── Helpers ──

    def _alloc_fh(self) -> int:
        """Allocate a new file handle."""
        fh = self._next_fh
        self._next_fh += 1
        return fh

    @staticmethod
    def _dir_stat() -> dict:
        """Return stat dict for a directory."""
        return {
            "st_mode": stat.S_IFDIR | 0o755,
            "st_nlink": 2,
            "st_size": 4096,
            "st_uid": _DEFAULT_UID,
            "st_gid": _DEFAULT_GID,
            "st_atime": time.time(),
            "st_mtime": time.time(),
            "st_ctime": time.time(),
        }

    @staticmethod
    def _file_stat(size: int) -> dict:
        """Return stat dict for a file."""
        return {
            "st_mode": stat.S_IFREG | 0o644,
            "st_nlink": 1,
            "st_size": size,
            "st_uid": _DEFAULT_UID,
            "st_gid": _DEFAULT_GID,
            "st_atime": time.time(),
            "st_mtime": time.time(),
            "st_ctime": time.time(),
        }

    @staticmethod
    def _raise(code: int, context: str = "") -> None:
        """Raise a FUSE error."""
        raise FuseOSError(code)  # type: ignore[name-defined]


# FuseOSError is provided by fusepy at runtime
try:
    from fuse import FuseOSError
except ImportError:
    # For testing without fusepy installed
    class FuseOSError(OSError):  # type: ignore[no-redef]
        pass
