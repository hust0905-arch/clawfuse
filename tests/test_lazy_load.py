"""Tests for DirTree lazy loading, ensure_loaded, and background_full_load."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, call

import pytest

from clawfuse.dirtree import DirTree, FileMeta


# ── Helpers ──

def _make_file(file_id: str, name: str, parent_id: str, is_dir: bool = False, size: int = 100) -> dict:
    """Create a Drive Kit file dict."""
    return {
        "id": file_id,
        "fileName": name,
        "mimeType": "application/vnd.huawei-apps.folder" if is_dir else "text/plain",
        "sha256": "abc" if not is_dir else "",
        "size": size,
        "parentFolder": [{"id": parent_id}],
        "modifiedTime": "2026-04-25T10:00:00Z",
    }


def _setup_list_files(mock_client: MagicMock, responses: dict[str, list[dict]]) -> None:
    """Configure mock_client.list_files to return specific files per parent_folder.

    responses: {parent_folder_id: [file_dict, ...]}
    """
    def _list_files(parent_folder=None, page_size=200, fields=None, cursor=None):
        if parent_folder and parent_folder in responses:
            return {"files": responses[parent_folder], "nextCursor": None}
        return {"files": [], "nextCursor": None}

    mock_client.list_files.side_effect = _list_files


# ── load_dir tests ──


def test_load_dir_basic(mock_client: MagicMock) -> None:
    """load_dir loads a single directory's children."""
    files = [
        _make_file("f1", "a.txt", "root"),
        _make_file("f2", "b.txt", "root"),
    ]
    _setup_list_files(mock_client, {"root": files})

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    tree.load_dir("root")

    assert tree.loaded_dir_count == 1
    assert tree.file_count == 2
    assert tree.resolve("/a.txt") is not None
    assert tree.resolve("/b.txt") is not None


def test_load_dir_idempotent(mock_client: MagicMock) -> None:
    """load_dir on same dir_id only calls API once."""
    files = [_make_file("f1", "a.txt", "root")]
    _setup_list_files(mock_client, {"root": files})

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    tree.load_dir("root")
    tree.load_dir("root")
    tree.load_dir("root")

    # list_files should only be called once
    assert mock_client.list_files.call_count == 1


def test_load_dir_nested(mock_client: MagicMock) -> None:
    """load_dir two levels deep."""
    root_files = [
        _make_file("dir1", "subdir", "root", is_dir=True),
        _make_file("f1", "top.txt", "root"),
    ]
    subdir_files = [
        _make_file("f2", "nested.txt", "dir1"),
    ]
    _setup_list_files(mock_client, {"root": root_files, "dir1": subdir_files})

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    tree.load_dir("root")
    assert tree.resolve("/subdir") is not None
    assert tree.resolve("/top.txt") is not None
    # nested.txt not loaded yet
    assert tree.resolve("/subdir/nested.txt") is None

    tree.load_dir("dir1")
    assert tree.resolve("/subdir/nested.txt") is not None
    assert tree.loaded_dir_count == 2


def test_load_dir_pagination(mock_client: MagicMock) -> None:
    """load_dir handles paginated API responses."""
    call_count = 0

    def _list_files(parent_folder=None, page_size=200, fields=None, cursor=None):
        nonlocal call_count
        call_count += 1
        if cursor is None:
            return {
                "files": [_make_file("f1", "a.txt", "root")],
                "nextCursor": "page2",
            }
        return {
            "files": [_make_file("f2", "b.txt", "root")],
            "nextCursor": None,
        }

    mock_client.list_files.side_effect = _list_files

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    tree.load_dir("root")

    assert tree.file_count == 2
    assert tree.resolve("/a.txt") is not None
    assert tree.resolve("/b.txt") is not None
    assert call_count == 2


# ── ensure_loaded tests ──


def test_ensure_loaded_root(mock_client: MagicMock) -> None:
    """ensure_loaded('/') loads root directory."""
    files = [_make_file("f1", "a.txt", "root")]
    _setup_list_files(mock_client, {"root": files})

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    tree.ensure_loaded("/")

    assert tree.loaded_dir_count == 1
    assert tree.list_dir("/") == ["a.txt"]


def test_ensure_loaded_deep_path(mock_client: MagicMock) -> None:
    """ensure_loaded walks from root to deep directory, loading each level."""
    root_files = [_make_file("dir1", "data", "root", is_dir=True)]
    dir1_files = [_make_file("dir2", "reports", "dir1", is_dir=True)]
    dir2_files = [_make_file("f1", "report.csv", "dir2")]

    _setup_list_files(mock_client, {
        "root": root_files,
        "dir1": dir1_files,
        "dir2": dir2_files,
    })

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    tree.ensure_loaded("/data/reports")

    assert tree.loaded_dir_count == 3
    assert tree.resolve("/data/reports") is not None
    assert tree.resolve("/data/reports/report.csv") is not None


def test_ensure_loaded_skips_already_loaded(mock_client: MagicMock) -> None:
    """ensure_loaded skips levels already loaded by background or previous calls."""
    root_files = [_make_file("dir1", "data", "root", is_dir=True)]
    dir1_files = [_make_file("dir2", "sub", "dir1", is_dir=True)]
    dir2_files = [_make_file("f1", "file.txt", "dir2")]

    _setup_list_files(mock_client, {
        "root": root_files,
        "dir1": dir1_files,
        "dir2": dir2_files,
    })

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)

    # First call: loads root, dir1, dir2
    tree.ensure_loaded("/data/sub")
    assert mock_client.list_files.call_count == 3

    # Second call: everything already loaded, no new API calls
    mock_client.list_files.reset_mock()
    tree.ensure_loaded("/data/sub")
    assert mock_client.list_files.call_count == 0


def test_ensure_loaded_partial(mock_client: MagicMock) -> None:
    """ensure_loaded on partially-loaded tree only loads missing levels."""
    root_files = [_make_file("dir1", "data", "root", is_dir=True)]
    dir1_files = [_make_file("dir2", "sub", "dir1", is_dir=True)]
    dir2_files = [_make_file("f1", "file.txt", "dir2")]

    _setup_list_files(mock_client, {
        "root": root_files,
        "dir1": dir1_files,
        "dir2": dir2_files,
    })

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)

    # Load root only
    tree.load_dir("root")
    assert mock_client.list_files.call_count == 1

    # Now ensure_loaded should only load dir1 and dir2 (not root again)
    tree.ensure_loaded("/data/sub")
    assert mock_client.list_files.call_count == 3  # root + dir1 + dir2


def test_ensure_loaded_nonexistent_path(mock_client: MagicMock) -> None:
    """ensure_loaded on non-existent path loads what it can without error."""
    root_files = [_make_file("f1", "real.txt", "root")]
    _setup_list_files(mock_client, {"root": root_files})

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    # Should not raise, even though /no/such/path doesn't exist
    tree.ensure_loaded("/no/such/path")

    # Root was loaded, but /no doesn't exist
    assert tree.loaded_dir_count == 1
    assert tree.resolve("/real.txt") is not None
    assert tree.resolve("/no") is None


# ── background_full_load tests ──


def test_background_full_load(mock_client: MagicMock) -> None:
    """background_full_load BFS loads all directories."""
    root_files = [
        _make_file("dir1", "data", "root", is_dir=True),
        _make_file("dir2", "docs", "root", is_dir=True),
        _make_file("f1", "readme.txt", "root"),
    ]
    dir1_files = [_make_file("f2", "report.csv", "dir1")]
    dir2_files = [_make_file("f3", "guide.md", "dir2")]

    _setup_list_files(mock_client, {
        "root": root_files,
        "dir1": dir1_files,
        "dir2": dir2_files,
    })

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    tree.background_full_load()

    assert tree.bg_complete is True
    assert tree.loaded_dir_count == 3  # root, dir1, dir2
    assert tree.file_count == 5  # dir1, dir2, readme, report, guide
    assert tree.resolve("/data/report.csv") is not None
    assert tree.resolve("/docs/guide.md") is not None
    assert tree.resolve("/readme.txt") is not None


def test_background_full_load_deep(mock_client: MagicMock) -> None:
    """background_full_load handles 3-level deep nesting."""
    root_files = [_make_file("d1", "a", "root", is_dir=True)]
    d1_files = [_make_file("d2", "b", "d1", is_dir=True)]
    d2_files = [_make_file("d3", "c", "d2", is_dir=True)]
    d3_files = [_make_file("f1", "deep.txt", "d3")]

    _setup_list_files(mock_client, {
        "root": root_files,
        "d1": d1_files,
        "d2": d2_files,
        "d3": d3_files,
    })

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    tree.background_full_load()

    assert tree.bg_complete is True
    assert tree.loaded_dir_count == 4
    assert tree.resolve("/a/b/c/deep.txt") is not None


def test_background_full_load_empty(mock_client: MagicMock) -> None:
    """background_full_load on empty drive completes immediately."""
    _setup_list_files(mock_client, {"root": []})

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    tree.background_full_load()

    assert tree.bg_complete is True
    assert tree.loaded_dir_count == 1  # root itself
    assert tree.file_count == 0


# ── Concurrent tests ──


def test_concurrent_load_dir_same_dir(mock_client: MagicMock) -> None:
    """Multiple threads loading same dir only calls API once."""
    files = [_make_file("f1", "a.txt", "root")]
    _setup_list_files(mock_client, {"root": files})

    # Simulate slow API
    original_side_effect = mock_client.list_files.side_effect

    def slow_list(*args, **kwargs):
        time.sleep(0.1)
        return original_side_effect(*args, **kwargs)

    mock_client.list_files.side_effect = slow_list

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    threads = [threading.Thread(target=tree.load_dir, args=("root",)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    # API should only be called once despite 5 threads
    assert mock_client.list_files.call_count == 1
    assert tree.loaded_dir_count == 1
    assert tree.resolve("/a.txt") is not None


def test_user_request_priority(mock_client: MagicMock) -> None:
    """User request can load a dir before background thread reaches it."""
    root_files = [
        _make_file("d1", "data", "root", is_dir=True),
        _make_file("d2", "docs", "root", is_dir=True),
    ]
    d1_files = [_make_file("f1", "report.csv", "d1")]
    d2_files = [_make_file("f2", "guide.md", "d2")]

    _setup_list_files(mock_client, {
        "root": root_files,
        "d1": d1_files,
        "d2": d2_files,
    })

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)

    # User requests /data before background starts
    tree.ensure_loaded("/data")
    assert tree.resolve("/data/report.csv") is not None

    # Now background loads everything — should skip /data (already loaded)
    call_count_before = mock_client.list_files.call_count
    tree.background_full_load()
    call_count_after = mock_client.list_files.call_count

    # Should have loaded d2 (1 more call), root and d1 skipped
    assert call_count_after - call_count_before == 1
    assert tree.bg_complete is True


# ── FUSE integration tests ──


def test_fuse_getattr_triggers_ensure_loaded() -> None:
    """FUSE getattr calls ensure_loaded when path not found."""
    from clawfuse.cache import ContentCache
    from clawfuse.client import DriveKitClient
    from clawfuse.fuse import ClawFUSE
    from clawfuse.writebuf import WriteBuffer

    mock_client = MagicMock(spec=DriveKitClient)
    mock_client.list_all_files.return_value = []
    mock_client.list_files.return_value = {
        "files": [_make_file("f1", "hello.txt", "root")],
        "nextCursor": None,
    }

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    cache = MagicMock(spec=ContentCache)
    writebuf = MagicMock(spec=WriteBuffer)

    fuse = ClawFUSE(mock_client, tree, cache, writebuf, root_folder="root")

    # getattr on unloaded tree should trigger ensure_loaded
    result = fuse.getattr("/hello.txt")
    assert result is not None
    assert result["st_size"] == 100


def test_fuse_readdir_triggers_ensure_loaded() -> None:
    """FUSE readdir calls ensure_loaded."""
    from clawfuse.cache import ContentCache
    from clawfuse.client import DriveKitClient
    from clawfuse.fuse import ClawFUSE
    from clawfuse.writebuf import WriteBuffer

    mock_client = MagicMock(spec=DriveKitClient)
    mock_client.list_files.return_value = {
        "files": [_make_file("f1", "a.txt", "root"), _make_file("f2", "b.txt", "root")],
        "nextCursor": None,
    }

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    cache = MagicMock(spec=ContentCache)
    writebuf = MagicMock(spec=WriteBuffer)

    fuse = ClawFUSE(mock_client, tree, cache, writebuf, root_folder="root")

    entries = fuse.readdir("/", 0)
    assert "a.txt" in entries
    assert "b.txt" in entries


def test_fuse_getattr_nonexistent_after_load() -> None:
    """FUSE getattr raises ENOENT after ensure_loaded confirms file doesn't exist."""
    from clawfuse.cache import ContentCache
    from clawfuse.client import DriveKitClient
    from clawfuse.fuse import ClawFUSE
    from clawfuse.writebuf import WriteBuffer

    mock_client = MagicMock(spec=DriveKitClient)
    mock_client.list_files.return_value = {
        "files": [_make_file("f1", "real.txt", "root")],
        "nextCursor": None,
    }

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    cache = MagicMock(spec=ContentCache)
    writebuf = MagicMock(spec=WriteBuffer)

    fuse = ClawFUSE(mock_client, tree, cache, writebuf, root_folder="root")

    with pytest.raises(OSError):  # FuseOSError is OSError
        fuse.getattr("/nonexistent.txt")


# ── Legacy compatibility ──


def test_legacy_refresh_still_works(mock_client: MagicMock, sample_files: list[dict]) -> None:
    """Legacy refresh() still works as before."""
    mock_client.list_all_files.return_value = sample_files

    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    assert tree.file_count == 4
    assert tree.resolve("/data") is not None
    assert tree.resolve("/data/report.csv") is not None
