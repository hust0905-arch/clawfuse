"""Tests for FUSE operations."""

from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from clawfuse.cache import ContentCache
from clawfuse.dirtree import DirTree, FileMeta
from clawfuse.fuse import ClawFUSE, FuseOSError
from clawfuse.writebuf import WriteBuffer


@pytest.fixture
def fuse_setup(tmp_path: Path, mock_client: MagicMock, sample_files: list[dict]) -> ClawFUSE:
    """Create a fully initialized ClawFUSE instance for testing."""
    mock_client.list_all_files.return_value = sample_files

    dirtree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    dirtree.refresh()

    cache = ContentCache(tmp_path / "cache", max_bytes=1024 * 1024, max_files=100)
    writebuf = WriteBuffer(mock_client, tmp_path / "buf", drain_interval=60, max_retries=3)

    return ClawFUSE(
        client=mock_client,
        dirtree=dirtree,
        cache=cache,
        writebuf=writebuf,
        root_folder="applicationData",
    )


def test_getattr_root(fuse_setup: ClawFUSE) -> None:
    """getattr('/') returns directory stat."""
    stat_result = fuse_setup.getattr("/")
    assert stat_result["st_mode"] & 0o040000  # S_IFDIR
    assert stat_result["st_nlink"] == 2


def test_getattr_file(fuse_setup: ClawFUSE) -> None:
    """getattr for a file returns file stat with correct size."""
    stat_result = fuse_setup.getattr("/README.md")
    assert stat_result["st_mode"] & 0o100000  # S_IFREG
    assert stat_result["st_size"] == 512


def test_getattr_dir(fuse_setup: ClawFUSE) -> None:
    """getattr for a directory returns dir stat."""
    stat_result = fuse_setup.getattr("/data")
    assert stat_result["st_mode"] & 0o040000  # S_IFDIR


def test_getattr_nonexistent(fuse_setup: ClawFUSE) -> None:
    """getattr for nonexistent path raises ENOENT."""
    with pytest.raises(FuseOSError) as exc_info:
        fuse_setup.getattr("/nonexistent.txt")
    assert exc_info.value.errno == errno.ENOENT


def test_readdir_root(fuse_setup: ClawFUSE) -> None:
    """readdir('/') returns root children."""
    entries = fuse_setup.readdir("/", 0)
    assert "." in entries
    assert ".." in entries
    assert "data" in entries
    assert "docs" in entries
    assert "README.md" in entries


def test_readdir_subfolder(fuse_setup: ClawFUSE) -> None:
    """readdir('/data') returns subfolder children."""
    entries = fuse_setup.readdir("/data", 0)
    assert "report.csv" in entries


def test_open_file(fuse_setup: ClawFUSE) -> None:
    """open returns a file handle."""
    fh = fuse_setup.open("/README.md", 0)  # O_RDONLY
    assert fh > 0


def test_open_nonexistent(fuse_setup: ClawFUSE) -> None:
    """open raises ENOENT for nonexistent file."""
    with pytest.raises(FuseOSError) as exc_info:
        fuse_setup.open("/nope.txt", 0)
    assert exc_info.value.errno == errno.ENOENT


def test_read_cache_miss(fuse_setup: ClawFUSE, mock_client: MagicMock) -> None:
    """read downloads file from Drive Kit on cache miss."""
    fh = fuse_setup.open("/README.md", 0)
    content = fuse_setup.read("/README.md", 4096, 0, fh)
    assert content == b"hello world"
    mock_client.download_file.assert_called_once_with("file_readme")


def test_read_cache_hit(fuse_setup: ClawFUSE, mock_client: MagicMock) -> None:
    """Second read uses cache (no additional download)."""
    fh = fuse_setup.open("/README.md", 0)
    _ = fuse_setup.read("/README.md", 4096, 0, fh)
    mock_client.download_file.reset_mock()

    # Open again and read
    fh2 = fuse_setup.open("/README.md", 0)
    content = fuse_setup.read("/README.md", 4096, 0, fh2)
    assert content == b"hello world"
    mock_client.download_file.assert_not_called()


def test_read_with_offset(fuse_setup: ClawFUSE) -> None:
    """read with offset returns correct slice."""
    fh = fuse_setup.open("/README.md", 0)
    content = fuse_setup.read("/README.md", 5, 6, fh)  # offset=6, size=5
    assert content == b"world"


def test_write_and_flush(fuse_setup: ClawFUSE, mock_client: MagicMock) -> None:
    """write + flush enqueues to write buffer."""
    fh = fuse_setup.create("/new_file.txt", 0o644)

    written = fuse_setup.write("/new_file.txt", b"new content", 0, fh)
    assert written == len(b"new content")

    fuse_setup.flush("/new_file.txt", fh)

    # Should have enqueued
    assert fuse_setup._writebuf.pending_count == 1


def test_create_file(fuse_setup: ClawFUSE, mock_client: MagicMock) -> None:
    """create creates file on Drive Kit and adds to dirtree."""
    fh = fuse_setup.create("/new_test.txt", 0o644)

    assert fh > 0
    mock_client.create_file.assert_called_once()
    meta = fuse_setup._dirtree.resolve("/new_test.txt")
    assert meta is not None
    assert meta.id == "new_file_001"


def test_unlink(fuse_setup: ClawFUSE, mock_client: MagicMock) -> None:
    """unlink deletes file from Drive Kit and dirtree."""
    fuse_setup.unlink("/README.md")
    mock_client.delete_file.assert_called_once_with("file_readme")
    assert fuse_setup._dirtree.resolve("/README.md") is None


def test_mkdir(fuse_setup: ClawFUSE, mock_client: MagicMock) -> None:
    """mkdir creates folder on Drive Kit."""
    fuse_setup.mkdir("/new_folder", 0o755)
    mock_client.create_folder.assert_called_once()
    meta = fuse_setup._dirtree.resolve("/new_folder")
    assert meta is not None
    assert meta.is_dir is True


def test_rmdir(fuse_setup: ClawFUSE, mock_client: MagicMock) -> None:
    """rmdir removes empty directory."""
    fuse_setup.rmdir("/docs")
    mock_client.delete_file.assert_called_once_with("folder_docs")
    assert fuse_setup._dirtree.resolve("/docs") is None


def test_rmdir_nonempty(fuse_setup: ClawFUSE) -> None:
    """rmdir raises ENOTEMPTY for non-empty directory."""
    with pytest.raises(FuseOSError) as exc_info:
        fuse_setup.rmdir("/data")
    assert exc_info.value.errno == errno.ENOTEMPTY


def test_rename(fuse_setup: ClawFUSE, mock_client: MagicMock) -> None:
    """rename updates Drive Kit metadata and dirtree."""
    fuse_setup.rename("/README.md", "/docs/README.md")

    mock_client.update_metadata.assert_called_once()
    assert fuse_setup._dirtree.resolve("/README.md") is None
    meta = fuse_setup._dirtree.resolve("/docs/README.md")
    assert meta is not None
    assert meta.id == "file_readme"


def test_release_cleans_up(fuse_setup: ClawFUSE) -> None:
    """release cleans up file handle state."""
    fh = fuse_setup.open("/README.md", 0)
    fuse_setup.release("/README.md", fh)
    assert fh not in fuse_setup._fh_map


def test_statfs(fuse_setup: ClawFUSE) -> None:
    """statfs returns a dict."""
    result = fuse_setup.statfs("/")
    assert "f_bsize" in result
    assert "f_blocks" in result


def test_noop_operations(fuse_setup: ClawFUSE) -> None:
    """No-op operations don't raise."""
    fuse_setup.chmod("/README.md", 0o644)
    fuse_setup.chown("/README.md", 0, 0)
    fuse_setup.utimens("/README.md", None)
    fuse_setup.access("/README.md", 0)
