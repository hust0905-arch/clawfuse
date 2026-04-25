"""Tests for writebuf module."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from clawfuse.writebuf import FlushResult, WriteBuffer


def test_enqueue_creates_buf_file(tmp_path: Path, mock_client: MagicMock) -> None:
    """enqueue persists content to .buf file."""
    buf_dir = tmp_path / "buf"
    wb = WriteBuffer(mock_client, buf_dir, drain_interval=60, max_retries=3)

    wb.enqueue("file1", "/test.txt", b"hello world", "sha256_abc")

    assert (buf_dir / "file1.buf").exists()
    assert (buf_dir / "file1.wmeta").exists()
    assert wb.pending_count == 1


def test_enqueue_overwrites_existing(tmp_path: Path, mock_client: MagicMock) -> None:
    """enqueue on same file_id replaces the pending write."""
    buf_dir = tmp_path / "buf"
    wb = WriteBuffer(mock_client, buf_dir, drain_interval=60, max_retries=3)

    wb.enqueue("file1", "/test.txt", b"old content", "sha_old")
    wb.enqueue("file1", "/test.txt", b"new content", "sha_new")

    assert wb.pending_count == 1
    pending = wb.get_pending("file1")
    assert pending is not None
    assert pending.content == b"new content"


def test_has_pending(tmp_path: Path, mock_client: MagicMock) -> None:
    """has_pending reflects queue state."""
    buf_dir = tmp_path / "buf"
    wb = WriteBuffer(mock_client, buf_dir, drain_interval=60, max_retries=3)

    assert not wb.has_pending
    wb.enqueue("file1", "/test.txt", b"data", "sha")
    assert wb.has_pending


def test_get_pending(tmp_path: Path, mock_client: MagicMock) -> None:
    """get_pending returns correct write."""
    buf_dir = tmp_path / "buf"
    wb = WriteBuffer(mock_client, buf_dir, drain_interval=60, max_retries=3)

    wb.enqueue("file1", "/test.txt", b"data", "sha")
    pending = wb.get_pending("file1")
    assert pending is not None
    assert pending.file_id == "file1"
    assert pending.path == "/test.txt"
    assert pending.content == b"data"
    assert pending.status == "pending"


def test_drain_uploads_pending(tmp_path: Path, mock_client: MagicMock) -> None:
    """Background drain uploads pending writes."""
    buf_dir = tmp_path / "buf"
    wb = WriteBuffer(mock_client, buf_dir, drain_interval=0.1, max_retries=3)

    wb.enqueue("file1", "/test.txt", b"hello", "sha")

    # Start drain and wait for it to process
    wb.start_drain()
    time.sleep(0.5)
    wb.stop_drain()

    # Should have called update_file
    mock_client.update_file.assert_called_once_with("file1", b"hello")
    # .buf files should be cleaned up
    assert not (buf_dir / "file1.buf").exists()
    assert wb.pending_count == 0


def test_drain_creates_new_file(tmp_path: Path, mock_client: MagicMock) -> None:
    """Drain creates file when file_id is empty."""
    buf_dir = tmp_path / "buf"
    wb = WriteBuffer(mock_client, buf_dir, drain_interval=0.1, max_retries=3)

    wb.enqueue("", "/new.txt", b"new content", "sha")

    wb.start_drain()
    time.sleep(0.5)
    wb.stop_drain()

    # Should have called create_file for empty file_id
    mock_client.create_file.assert_called_once()


def test_flush_all_sync(tmp_path: Path, mock_client: MagicMock) -> None:
    """flush_all synchronously uploads all pending writes."""
    buf_dir = tmp_path / "buf"
    wb = WriteBuffer(mock_client, buf_dir, drain_interval=60, max_retries=3)

    wb.enqueue("f1", "/a.txt", b"aaa", "sha1")
    wb.enqueue("f2", "/b.txt", b"bbb", "sha2")

    result = wb.flush_all(timeout=10)

    assert result.succeeded == 2
    assert result.failed == 0
    assert wb.pending_count == 0
    assert not (buf_dir / "f1.buf").exists()
    assert not (buf_dir / "f2.buf").exists()


def test_flush_all_handles_failure(tmp_path: Path, mock_client: MagicMock) -> None:
    """flush_all retries and reports failures."""
    mock_client.update_file.side_effect = Exception("Network error")

    buf_dir = tmp_path / "buf"
    wb = WriteBuffer(mock_client, buf_dir, drain_interval=60, max_retries=2)

    wb.enqueue("f1", "/a.txt", b"data", "sha")
    result = wb.flush_all(timeout=10)

    assert result.failed == 1
    assert len(result.errors) == 1


def test_restore_from_disk(tmp_path: Path, mock_client: MagicMock) -> None:
    """Pending writes are restored from .buf files on restart."""
    buf_dir = tmp_path / "buf"

    # First instance: enqueue and persist
    wb1 = WriteBuffer(mock_client, buf_dir, drain_interval=60, max_retries=3)
    wb1.enqueue("f1", "/test.txt", b"crash data", "sha_f1")
    assert wb1.pending_count == 1

    # Simulate crash: don't call flush_all, just abandon wb1

    # Second instance: should restore
    wb2 = WriteBuffer(mock_client, buf_dir, drain_interval=60, max_retries=3)
    assert wb2.pending_count == 1
    pending = wb2.get_pending("f1")
    assert pending is not None
    assert pending.content == b"crash data"


def test_flush_result_dataclass() -> None:
    """FlushResult is a frozen dataclass."""
    result = FlushResult(total=5, succeeded=4, failed=1, errors=["err1"])
    assert result.total == 5
    assert result.succeeded == 4
    assert result.failed == 1
    with pytest.raises(AttributeError):
        result.total = 10  # type: ignore[misc]
