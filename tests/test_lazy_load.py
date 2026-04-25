"""Tests for DirTree lazy loading, ensure_loaded, and background_full_load."""

from __future__ import annotations

import threading
import time
from pathlib import Path
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


# ── Large-scale lazy loading performance ──


def _generate_large_tree(
    num_dirs: int = 100,
    files_per_dir: int = 20,
) -> dict[str, list[dict]]:
    """Generate a large tree for lazy loading perf tests.

    Returns {parent_folder_id: [child_items]} suitable for _setup_list_files.
    Structure: root -> dir_000 .. dir_099, each dir has files_per_dir files.
    """
    responses: dict[str, list[dict]] = {"root": []}

    for d in range(num_dirs):
        dir_id = f"dir_{d:04d}"
        # Add dir to root's children
        responses["root"].append(_make_file(dir_id, f"dir_{d:04d}", "root", is_dir=True))

        children: list[dict] = []
        # Each dir has some files
        for f in range(files_per_dir):
            children.append(_make_file(
                f"f_{d:04d}_{f:04d}",
                f"file_{f:04d}.txt",
                dir_id,
                size=1024 * (f + 1),
            ))
        # Some dirs have sub-dirs (every 10th)
        if d % 10 == 0:
            for sd in range(3):
                sub_id = f"subdir_{d:04d}_{sd}"
                children.append(_make_file(sub_id, f"sub_{sd}", dir_id, is_dir=True))
                # Sub-dir files
                sub_children = [
                    _make_file(f"sf_{d:04d}_{sd}_{sf}", f"sfile_{sf}.txt", sub_id, size=512)
                    for sf in range(5)
                ]
                responses[sub_id] = sub_children
        responses[dir_id] = children
    return responses


def test_lazy_load_2000_files_perf() -> None:
    """Lazy load 100 dirs × 20 files + sub-dirs = ~2200 files, measure total time."""
    responses = _generate_large_tree(num_dirs=100, files_per_dir=20)
    mock_client = MagicMock()
    _setup_list_files(mock_client, responses)

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)

    start = time.perf_counter()
    tree.background_full_load()
    elapsed = time.perf_counter() - start

    print(f"\n  Lazy load 2200+ items: {elapsed:.3f}s, {tree.file_count} items, {tree.loaded_dir_count} dirs")
    assert tree.bg_complete is True
    assert tree.file_count >= 2000
    # Mock API calls should be fast — no real network
    assert elapsed < 5.0, f"Too slow: {elapsed:.3f}s"


def test_ensure_loaded_deep_path_with_many_siblings() -> None:
    """ensure_loaded on deep path while many sibling dirs exist."""
    responses = _generate_large_tree(num_dirs=100, files_per_dir=10)
    mock_client = MagicMock()
    _setup_list_files(mock_client, responses)

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)

    # Access a sub-dir path: root -> dir_0010 -> sub_0
    start = time.perf_counter()
    tree.ensure_loaded("/dir_0010/sub_0")
    elapsed = time.perf_counter() - start

    # 3 dirs loaded: root, dir_0010, subdir_0010_0
    assert tree.loaded_dir_count == 3
    assert tree.resolve("/dir_0010/sub_0") is not None
    print(f"\n  ensure_loaded deep path (3 levels, 100 siblings): {elapsed*1000:.1f}ms, {tree.loaded_dir_count} dirs loaded")


def test_concurrent_ensure_loaded_many_paths() -> None:
    """Multiple threads ensure_loaded on different paths concurrently."""
    responses = _generate_large_tree(num_dirs=50, files_per_dir=10)
    mock_client = MagicMock()
    _setup_list_files(mock_client, responses)

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)

    # Simulate 10 threads accessing different directories simultaneously
    target_dirs = [f"/dir_{i:04d}" for i in range(0, 50, 5)]
    errors: list[str] = []

    def access_dir(dir_path: str) -> None:
        try:
            tree.ensure_loaded(dir_path)
            meta = tree.resolve(dir_path)
            assert meta is not None, f"Path {dir_path} not found after ensure_loaded"
        except Exception as e:
            errors.append(f"{dir_path}: {e}")

    start = time.perf_counter()
    threads = [threading.Thread(target=access_dir, args=(d,)) for d in target_dirs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    elapsed = time.perf_counter() - start

    assert not errors, f"Errors: {errors}"
    # Root + each dir loaded
    assert tree.loaded_dir_count >= len(target_dirs) + 1
    print(f"\n  {len(target_dirs)} concurrent ensure_loaded: {elapsed*1000:.1f}ms, {tree.loaded_dir_count} dirs loaded")


def test_user_request_during_background_load() -> None:
    """User accesses a dir while background_full_load is in progress."""
    responses = _generate_large_tree(num_dirs=100, files_per_dir=10)
    mock_client = MagicMock()

    # Add artificial delay to API calls so background and user overlap
    original_responses = dict(responses)

    def slow_list_files(parent_folder=None, page_size=200, fields=None, cursor=None):
        time.sleep(0.01)  # 10ms per API call
        if parent_folder and parent_folder in original_responses:
            return {"files": original_responses[parent_folder], "nextCursor": None}
        return {"files": [], "nextCursor": None}

    mock_client.list_files.side_effect = slow_list_files

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)

    # Start background load in a thread
    bg_thread = threading.Thread(target=tree.background_full_load, daemon=True)
    bg_thread.start()

    # Wait a bit for background to start
    time.sleep(0.05)

    # User accesses a specific directory — should get result even while background is running
    tree.ensure_loaded("/dir_0050")
    meta = tree.resolve("/dir_0050")
    assert meta is not None

    # Wait for background to finish
    bg_thread.join(timeout=30)
    assert tree.bg_complete is True

    print(f"\n  User request during background: dir_0050 resolved, bg loaded {tree.loaded_dir_count} dirs total")


def test_fuse_ops_on_large_lazy_tree(tmp_path: Path) -> None:
    """FUSE getattr/readdir on a large lazy-loaded tree."""
    from clawfuse.cache import ContentCache
    from clawfuse.client import DriveKitClient
    from clawfuse.fuse import ClawFUSE
    from clawfuse.writebuf import WriteBuffer

    responses = _generate_large_tree(num_dirs=50, files_per_dir=20)
    mock_client = MagicMock(spec=DriveKitClient)
    mock_client.list_all_files.return_value = []
    mock_client.download_file.return_value = b"cached content" * 100
    _setup_list_files(mock_client, responses)

    tree = DirTree(mock_client, root_folder="root", refresh_ttl=3600)
    cache = ContentCache(tmp_path / "cache", max_bytes=50 * 1024 * 1024, max_files=500)
    writebuf = MagicMock(spec=WriteBuffer)
    fuse = ClawFUSE(mock_client, tree, cache, writebuf, root_folder="root")

    # Phase 1: getattr on files across many directories
    start = time.perf_counter()
    resolved = 0
    for d in range(0, 50, 5):
        for f in range(5):
            path = f"/dir_{d:04d}/file_{f:04d}.txt"
            stat = fuse.getattr(path)
            if stat is not None:
                resolved += 1
    getattr_elapsed = time.perf_counter() - start

    # Phase 2: readdir on directories
    start = time.perf_counter()
    for d in range(0, 50, 10):
        path = f"/dir_{d:04d}"
        entries = fuse.readdir(path, 0)
        assert "." in entries
    readdir_elapsed = time.perf_counter() - start

    print(f"\n  FUSE getattr x{resolved} (lazy): {getattr_elapsed*1000:.1f}ms")
    print(f"  FUSE readdir x5 (lazy): {readdir_elapsed*1000:.1f}ms")
    print(f"  Total dirs loaded: {tree.loaded_dir_count}")

    assert resolved > 0
    assert tree.loaded_dir_count > 10  # Should have loaded multiple directories
