"""Write buffer with background drain to Drive Kit.

Buffers file writes locally (.buf files), then uploads them in a background
thread. Provides crash recovery by persisting writes to disk before returning.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .client import DriveKitClient
from .config import FOLDER_MIME
from .exceptions import SyncError

logger = logging.getLogger(__name__)


@dataclass
class PendingWrite:
    """A write waiting to be uploaded to Drive Kit."""

    file_id: str
    path: str
    content: bytes
    sha256: str
    queued_at: float
    retry_count: int = 0
    status: str = "pending"  # pending | uploading | failed


@dataclass(frozen=True)
class FlushResult:
    """Result of a flush_all operation."""

    total: int
    succeeded: int
    failed: int
    errors: list[str]


class WriteBuffer:
    """Write buffer with background drain thread."""

    def __init__(
        self,
        client: DriveKitClient,
        buffer_dir: Path,
        drain_interval: float = 5.0,
        max_retries: int = 3,
    ) -> None:
        self._client = client
        self._buffer_dir = buffer_dir
        self._drain_interval = drain_interval
        self._max_retries = max_retries

        # file_id → PendingWrite
        self._queue: dict[str, PendingWrite] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._drain_thread: threading.Thread | None = None

        # Create buffer directory
        self._buffer_dir.mkdir(parents=True, exist_ok=True)

        # Restore pending writes from disk
        self._restore_from_disk()

    def enqueue(self, file_id: str, path: str, content: bytes, sha256: str) -> None:
        """Add a write to the buffer queue."""
        pending = PendingWrite(
            file_id=file_id,
            path=path,
            content=content,
            sha256=sha256,
            queued_at=time.time(),
        )

        with self._lock:
            self._queue[file_id] = pending

        # Persist to disk for crash recovery
        self._write_buf_file(file_id, content)
        self._write_meta_file(file_id, path, sha256)

        logger.debug("Enqueued write: %s (%d bytes)", path, len(content))

    def start_drain(self) -> None:
        """Start the background drain thread."""
        if self._drain_thread is not None and self._drain_thread.is_alive():
            return

        self._stop_event.clear()
        self._drain_thread = threading.Thread(target=self._drain_loop, daemon=True)
        self._drain_thread.start()
        logger.info("Write buffer drain thread started (interval=%.1fs)", self._drain_interval)

    def stop_drain(self) -> None:
        """Stop the background drain thread."""
        self._stop_event.set()
        if self._drain_thread is not None and self._drain_thread.is_alive():
            self._drain_thread.join(timeout=30)
        logger.info("Write buffer drain thread stopped")

    def flush_all(self, timeout: float = 120.0) -> FlushResult:
        """Synchronously drain all pending writes. Called during pre-destroy."""
        start = time.monotonic()
        succeeded = 0
        failed = 0
        errors: list[str] = []

        # Stop background drain first
        self.stop_drain()

        while True:
            with self._lock:
                pending = [w for w in self._queue.values() if w.status in ("pending", "failed")]

            if not pending:
                break

            if (time.monotonic() - start) > timeout:
                remaining = len(pending)
                errors.append(f"Timeout after {timeout}s with {remaining} writes remaining")
                break

            for write in pending:
                if self._upload_one(write):
                    with self._lock:
                        self._queue.pop(write.file_id, None)
                    self._remove_buf_files(write.file_id)
                    succeeded += 1
                else:
                    if write.retry_count >= self._max_retries:
                        failed += 1
                        errors.append(f"Failed to sync {write.path} after {write.retry_count} retries")
                        with self._lock:
                            self._queue.pop(write.file_id, None)
                        # Keep .buf files for manual recovery
                    # else: will retry in next iteration

        elapsed = time.monotonic() - start
        logger.info(
            "flush_all complete: %d succeeded, %d failed in %.1fs",
            succeeded,
            failed,
            elapsed,
        )

        total = succeeded + failed
        return FlushResult(total=total, succeeded=succeeded, failed=failed, errors=errors)

    def get_pending(self, file_id: str) -> PendingWrite | None:
        """Get pending write for a file."""
        with self._lock:
            return self._queue.get(file_id)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return len(self._queue) > 0

    # ── Internal ──

    def _drain_loop(self) -> None:
        """Background thread: periodically upload pending writes."""
        while not self._stop_event.is_set():
            self._stop_event.wait(self._drain_interval)
            if self._stop_event.is_set():
                break
            self._drain_one_batch()

    def _drain_one_batch(self) -> None:
        """Upload all pending writes in one batch."""
        with self._lock:
            pending = [w for w in list(self._queue.values()) if w.status == "pending"]

        for write in pending:
            if self._stop_event.is_set():
                break
            if self._upload_one(write):
                with self._lock:
                    self._queue.pop(write.file_id, None)
                self._remove_buf_files(write.file_id)

    def _upload_one(self, write: PendingWrite) -> bool:
        """Upload a single pending write. Returns True on success."""
        try:
            write.status = "uploading"

            if write.file_id:
                # Update existing file
                result = self._client.update_file(write.file_id, write.content)
            else:
                # Create new file
                name = Path(write.path).name
                parent_id = self._guess_parent_id(write.path)
                result = self._client.create_file(
                    filename=name,
                    content=write.content,
                    parent_folder=parent_id,
                )
                write.file_id = result.get("id", write.file_id)

            logger.info("Uploaded: %s (%d bytes)", write.path, len(write.content))
            return True

        except Exception as e:
            write.retry_count += 1
            write.status = "pending" if write.retry_count < self._max_retries else "failed"
            # Backoff: wait between retries to avoid hammering a failing API.
            # Use _stop_event.wait() instead of time.sleep() so the drain
            # thread can respond to stop_drain() during backoff.
            if write.status == "pending":
                backoff = min(2 ** write.retry_count, 10)
                logger.debug("Retry backoff: %.1fs for %s", backoff, write.path)
                self._stop_event.wait(timeout=backoff)
            logger.warning(
                "Upload failed for %s (attempt %d/%d): %s",
                write.path,
                write.retry_count,
                self._max_retries,
                e,
            )
            return False

    def _guess_parent_id(self, path: str) -> str:
        """Guess parent folder ID from path. Returns 'applicationData' as fallback."""
        # This is a simplified version — DirTree should provide the real parent_id
        # For now, use root folder
        return "applicationData"

    def _write_buf_file(self, file_id: str, content: bytes) -> None:
        """Write content to .buf file for crash recovery."""
        buf_path = self._buffer_dir / f"{file_id}.buf"
        tmp_path = buf_path.with_suffix(".buf.tmp")
        try:
            tmp_path.write_bytes(content)
            tmp_path.rename(buf_path)
        except OSError as e:
            logger.error("Failed to write .buf file for %s: %s", file_id, e)

    def _write_meta_file(self, file_id: str, path: str, sha256: str) -> None:
        """Write metadata for a pending write."""
        meta_path = self._buffer_dir / f"{file_id}.wmeta"
        meta = {"file_id": file_id, "path": path, "sha256": sha256, "queued_at": time.time()}
        try:
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
        except OSError as e:
            logger.error("Failed to write .wmeta file for %s: %s", file_id, e)

    def _remove_buf_files(self, file_id: str) -> None:
        """Remove .buf and .wmeta files after successful upload."""
        (self._buffer_dir / f"{file_id}.buf").unlink(missing_ok=True)
        (self._buffer_dir / f"{file_id}.wmeta").unlink(missing_ok=True)

    def _restore_from_disk(self) -> None:
        """Restore pending writes from .buf + .wmeta files."""
        meta_files = list(self._buffer_dir.glob("*.wmeta"))
        if not meta_files:
            return

        restored = 0
        for meta_path in meta_files:
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                file_id = data["file_id"]
                buf_path = self._buffer_dir / f"{file_id}.buf"

                if not buf_path.exists():
                    meta_path.unlink(missing_ok=True)
                    continue

                content = buf_path.read_bytes()
                pending = PendingWrite(
                    file_id=file_id,
                    path=data["path"],
                    content=content,
                    sha256=data.get("sha256", hashlib.sha256(content).hexdigest()),
                    queued_at=data.get("queued_at", 0),
                )
                self._queue[file_id] = pending
                restored += 1
            except (json.JSONDecodeError, OSError, KeyError) as e:
                logger.warning("Skipping corrupt write meta %s: %s", meta_path, e)

        if restored:
            logger.info("Restored %d pending writes from disk", restored)
