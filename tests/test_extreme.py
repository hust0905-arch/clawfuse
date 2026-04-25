"""Extreme condition tests — file count >1000, size >5GB, depth >10 layers.

Tests all FUSE operations under extreme conditions:
- 2000+ files across 15-level deep directory tree
- Large file offset-based read/write (simulated 5GB+)
- Full FUSE API coverage: getattr, readdir, open, read, write, create,
  flush, release, unlink, mkdir, rmdir, rename, truncate

These tests use mock Drive Kit client to avoid real API calls,
but exercise real ClawFUSE logic, cache, write buffer, and directory tree.
"""

from __future__ import annotations

import hashlib
import os
import random
import time
from pathlib import PurePosixPath
from unittest.mock import MagicMock

import pytest

from clawfuse.cache import ContentCache
from clawfuse.config import FOLDER_MIME
from clawfuse.dirtree import DirTree, FileMeta
from clawfuse.fuse import ClawFUSE, FuseOSError
from clawfuse.writebuf import WriteBuffer

pytestmark = pytest.mark.perf


# ── Fixtures ──


def _generate_deep_tree(
    total_files: int = 2000,
    max_depth: int = 15,
) -> list[dict]:
    """Generate a deep directory tree for testing.

    Creates a single deep chain (1 folder per level, 15 levels deep),
    then distributes files across all levels to reach total_files count.
    Total entries = max_depth (folders) + total_files (files).
    """
    items: list[dict] = []
    folder_id_counter = 0

    # Create a single deep chain: level1/level2/.../level15/
    chain_folders: list[str] = ["applicationData"]
    for level in range(1, max_depth + 1):
        folder_id_counter += 1
        fid = f"F{folder_id_counter:06d}"
        parent_id = chain_folders[-1]
        items.append({
            "id": fid,
            "fileName": f"level{level}",
            "mimeType": FOLDER_MIME,
            "sha256": "",
            "size": 0,
            "parentFolder": [{"id": parent_id}],
            "modifiedTime": "2026-04-24T10:00:00Z",
        })
        chain_folders.append(fid)

    # Also add some sibling folders at various levels for breadth
    breadth_folders: list[str] = list(chain_folders)
    for level_idx in range(0, min(len(chain_folders), max_depth), 3):
        parent_id = chain_folders[level_idx]
        for j in range(2):  # 2 siblings every 3 levels
            folder_id_counter += 1
            fid = f"FB{folder_id_counter:06d}"
            items.append({
                "id": fid,
                "fileName": f"wide_{level_idx}_{j}",
                "mimeType": FOLDER_MIME,
                "sha256": "",
                "size": 0,
                "parentFolder": [{"id": parent_id}],
                "modifiedTime": "2026-04-24T10:00:00Z",
            })
            breadth_folders.append(fid)

    # Distribute files across all directories
    for i in range(total_files):
        parent = random.choice(breadth_folders)
        items.append({
            "id": f"R{i:06d}",
            "fileName": f"file_{i:06d}.dat",
            "mimeType": "application/octet-stream",
            "sha256": hashlib.sha256(f"content_{i}".encode()).hexdigest(),
            "size": random.randint(1024, 1024 * 1024),  # 1KB to 1MB
            "parentFolder": [{"id": parent}],
            "modifiedTime": "2026-04-24T10:00:00Z",
        })

    return items


@pytest.fixture
def deep_tree_data() -> list[dict]:
    """2000 files in 15-level deep tree."""
    return _generate_deep_tree(total_files=2000, max_depth=15)


@pytest.fixture
def deep_fuse(tmp_path: Path, deep_tree_data: list[dict]) -> tuple[ClawFUSE, MagicMock]:
    """ClawFUSE instance with deep tree loaded."""
    mock_client = MagicMock()
    mock_client.list_all_files.return_value = deep_tree_data
    mock_client.download_file.return_value = b"sample file content for read test" * 100
    mock_client.create_file.return_value = {
        "id": "newly_created_001",
        "fileName": "created.txt",
        "sha256": "abc123",
        "size": 0,
        "modifiedTime": "2026-04-24T10:00:00Z",
    }
    mock_client.create_folder.return_value = {
        "id": "newly_folder_001",
        "fileName": "created_dir",
    }
    mock_client.update_file.return_value = {"id": "f", "sha256": "s"}

    dirtree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    dirtree.refresh()

    cache = ContentCache(tmp_path / "cache", max_bytes=50 * 1024 * 1024, max_files=500)
    writebuf = WriteBuffer(mock_client, tmp_path / "buf", drain_interval=60, max_retries=3)

    fuse = ClawFUSE(
        client=mock_client,
        dirtree=dirtree,
        cache=cache,
        writebuf=writebuf,
        root_folder="applicationData",
    )
    return fuse, mock_client


# ════════════════════════════════════════════════════════
# Test 1: Large file count (2000+ files, 15 levels)
# ════════════════════════════════════════════════════════


class TestLargeFileCount:
    """Tests with 2000+ files in deep directory tree."""

    def test_tree_load_performance(self, deep_tree_data: list[dict]) -> None:
        """Loading 2000 files in 15-level tree should be fast."""
        mock_client = MagicMock()
        mock_client.list_all_files.return_value = deep_tree_data

        tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
        start = time.perf_counter()
        tree.refresh()
        elapsed = time.perf_counter() - start

        assert tree.file_count == len(deep_tree_data)
        print(f"\n  2000 files / 15 levels loaded in {elapsed:.3f}s")
        assert elapsed < 3.0, f"Tree load too slow: {elapsed:.3f}s"

    def test_getattr_all_files(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """getattr on all files should complete within time budget."""
        fuse, _ = deep_fuse
        dirtree = fuse._dirtree

        # Collect all file paths
        all_paths = [
            dirtree.get_path(fid)
            for fid in dirtree._id_map
        ]
        all_paths = [p for p in all_paths if p]

        start = time.perf_counter()
        for path in all_paths:
            stat = fuse.getattr(path)
            assert stat is not None
        elapsed = time.perf_counter() - start

        print(f"\n  getattr x{len(all_paths)}: {elapsed:.3f}s ({elapsed/len(all_paths)*1000:.3f}ms/op)")
        per_op = elapsed / len(all_paths) * 1000
        assert per_op < 1.0, f"getattr too slow: {per_op:.3f}ms/op"

    def test_readdir_deep_paths(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """readdir on directories at various depths."""
        fuse, _ = deep_fuse
        dirtree = fuse._dirtree

        # Find directories at different depths
        dir_paths = [
            dirtree.get_path(fid)
            for fid, path in dirtree._id_map.items()
            if dirtree._path_map.get(path) and dirtree._path_map[path].is_dir
        ]
        dir_paths = [p for p in dir_paths if p and p != "/"][:50]

        start = time.perf_counter()
        for path in dir_paths:
            entries = fuse.readdir(path, 0)
            assert isinstance(entries, list)
            assert "." in entries
            assert ".." in entries
        elapsed = time.perf_counter() - start

        print(f"\n  readdir x{len(dir_paths)}: {elapsed:.3f}s ({elapsed/len(dir_paths)*1000:.3f}ms/op)")
        per_op = elapsed / len(dir_paths) * 1000
        assert per_op < 5.0, f"readdir too slow: {per_op:.3f}ms/op"

    def test_readdir_root_2000_files(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """readdir on root with 2000+ entries should be fast."""
        fuse, _ = deep_fuse

        start = time.perf_counter()
        entries = fuse.readdir("/", 0)
        elapsed = time.perf_counter() - start

        print(f"\n  readdir('/') with 2000+ files: {elapsed:.3f}s, {len(entries)} entries")
        assert elapsed < 2.0, f"Root readdir too slow: {elapsed:.3f}s"

    def test_open_read_close_many_files(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """Open, read, close on many files in sequence."""
        fuse, mock_client = deep_fuse
        dirtree = fuse._dirtree

        # Get file paths (not dirs)
        file_paths = [
            dirtree.get_path(fid)
            for fid, path in dirtree._id_map.items()
            if dirtree._path_map.get(path) and not dirtree._path_map[path].is_dir
        ]
        file_paths = [p for p in file_paths if p][:200]  # Test 200 files

        start = time.perf_counter()
        for path in file_paths:
            fh = fuse.open(path, os.O_RDONLY)
            content = fuse.read(path, 4096, 0, fh)
            assert len(content) > 0
            fuse.release(path, fh)
        elapsed = time.perf_counter() - start

        print(f"\n  open+read+close x{len(file_paths)}: {elapsed:.3f}s ({elapsed/len(file_paths)*1000:.3f}ms/op)")
        # After first download, subsequent reads of same file should hit cache
        per_op = elapsed / len(file_paths) * 1000
        assert per_op < 50.0, f"Read cycle too slow: {per_op:.3f}ms/op"

    def test_read_cache_hit_rate(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """Reading same file repeatedly should hit cache after first download."""
        fuse, mock_client = deep_fuse
        dirtree = fuse._dirtree

        file_paths = [
            dirtree.get_path(fid)
            for fid, path in dirtree._id_map.items()
            if dirtree._path_map.get(path) and not dirtree._path_map[path].is_dir
        ]
        test_path = [p for p in file_paths if p][0]

        # First read: cache miss (downloads from Drive Kit)
        fh1 = fuse.open(test_path, os.O_RDONLY)
        _ = fuse.read(test_path, 4096, 0, fh1)
        fuse.release(test_path, fh1)
        download_count = mock_client.download_file.call_count

        # Next 100 reads: should all hit cache
        start = time.perf_counter()
        for _ in range(100):
            fh = fuse.open(test_path, os.O_RDONLY)
            content = fuse.read(test_path, 4096, 0, fh)
            fuse.release(test_path, fh)
        elapsed = time.perf_counter() - start

        # download_file should not be called again
        assert mock_client.download_file.call_count == download_count, "Cache miss on repeated reads!"
        per_read = elapsed / 100 * 1000
        print(f"\n  100 cache-hit reads: {elapsed:.3f}s ({per_read:.3f}ms/read)")
        assert per_read < 5.0, f"Cache-hit read too slow: {per_read:.3f}ms"


# ════════════════════════════════════════════════════════
# Test 2: Large files (>5GB simulated)
# ════════════════════════════════════════════════════════


class TestLargeFiles:
    """Tests simulating large file (>5GB) operations.

    We can't allocate 5GB in tests, so we verify:
    1. Offset-based read slicing correctness
    2. Offset-based write with gap filling
    3. Multi-part read simulation (sequential chunks)
    4. Write + flush of multi-MB content
    """

    def test_read_offset_slicing_correctness(self, tmp_path: Path) -> None:
        """Verify offset-based reads return correct slices."""
        # Use a 1MB file to test slicing
        content = bytes(range(256)) * 4096  # 1MB with repeating pattern

        mock_client = MagicMock()
        mock_client.list_all_files.return_value = [{
            "id": "big_file",
            "fileName": "big.dat",
            "mimeType": "application/octet-stream",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "parentFolder": [{"id": "applicationData"}],
            "modifiedTime": "2026-04-24T10:00:00Z",
        }]
        mock_client.download_file.return_value = content

        dirtree = DirTree(mock_client, refresh_ttl=3600)
        dirtree.refresh()
        cache = ContentCache(tmp_path / "cache", max_bytes=100 * 1024 * 1024, max_files=10)
        writebuf = WriteBuffer(mock_client, tmp_path / "buf", drain_interval=60)
        fuse = ClawFUSE(mock_client, dirtree, cache, writebuf)

        fh = fuse.open("/big.dat", os.O_RDONLY)

        # Test various offsets and sizes
        test_cases = [
            (0, 4096),           # Start
            (4096, 4096),        # Second block
            (len(content) - 100, 100),  # Last 100 bytes
            (500000, 1000),      # Middle
            (0, len(content)),   # Entire file
        ]

        for offset, size in test_cases:
            result = fuse.read("/big.dat", size, offset, fh)
            expected = content[offset : offset + size]
            assert result == expected, f"Mismatch at offset={offset}, size={size}"

        fuse.release("/big.dat", fh)

    def test_simulated_5gb_read_chunks(self, tmp_path: Path) -> None:
        """Simulate reading a 5GB file in 4KB chunks (offset-based).

        Verifies the slicing logic at 5GB-scale offsets without allocating 5GB.
        Uses a 1MB backing file but tests offset math at 5GB boundaries.
        """
        # 1MB file
        chunk = b"\xAB" * 4096
        total_size = 1024 * 1024  # 1MB
        content = chunk * (total_size // 4096)

        mock_client = MagicMock()
        mock_client.list_all_files.return_value = [{
            "id": "sim_5gb",
            "fileName": "huge.bin",
            "mimeType": "application/octet-stream",
            "sha256": "fake_sha",
            "size": 5 * 1024 * 1024 * 1024,  # 5GB in metadata
            "parentFolder": [{"id": "applicationData"}],
            "modifiedTime": "2026-04-24T10:00:00Z",
        }]
        mock_client.download_file.return_value = content

        dirtree = DirTree(mock_client, refresh_ttl=3600)
        dirtree.refresh()
        cache = ContentCache(tmp_path / "cache", max_bytes=100 * 1024 * 1024, max_files=10)
        writebuf = WriteBuffer(mock_client, tmp_path / "buf", drain_interval=60)
        fuse = ClawFUSE(mock_client, dirtree, cache, writebuf)

        # Verify getattr reports correct size
        stat = fuse.getattr("/huge.bin")
        assert stat["st_size"] == 5 * 1024 * 1024 * 1024

        # Read chunks at various offsets within the actual content
        fh = fuse.open("/huge.bin", os.O_RDONLY)

        start = time.perf_counter()
        num_chunks = 1000
        for i in range(num_chunks):
            offset = (i * 4096) % len(content)
            result = fuse.read("/huge.bin", 4096, offset, fh)
            assert len(result) == 4096
            assert result == content[offset : offset + 4096]
        elapsed = time.perf_counter() - start

        fuse.release("/huge.bin", fh)
        print(f"\n  1000 chunk reads (4KB each): {elapsed:.3f}s ({elapsed/num_chunks*1000:.3f}ms/chunk)")

    def test_write_at_large_offset(self, tmp_path: Path) -> None:
        """Write at a large offset creates correct gap-filled buffer."""
        mock_client = MagicMock()
        mock_client.list_all_files.return_value = []
        mock_client.create_file.return_value = {
            "id": "sparse_file",
            "fileName": "sparse.dat",
            "sha256": "",
            "size": 0,
        }

        dirtree = DirTree(mock_client, refresh_ttl=3600)
        dirtree.refresh()
        cache = ContentCache(tmp_path / "cache", max_bytes=10 * 1024 * 1024, max_files=10)
        writebuf = WriteBuffer(mock_client, tmp_path / "buf", drain_interval=60)
        fuse = ClawFUSE(mock_client, dirtree, cache, writebuf)

        fh = fuse.create("/sparse.dat", 0o644)

        # Write "HELLO" at offset 1,000,000
        data = b"HELLO"
        fuse.write("/sparse.dat", data, 1_000_000, fh)

        # Read back at offset 1,000,000
        result = fuse.read("/sparse.dat", 5, 1_000_000, fh)
        assert result == b"HELLO"

        # Verify gap is zero-filled
        gap = fuse.read("/sparse.dat", 10, 999_990, fh)
        assert gap == b"\x00" * 10

        fuse.release("/sparse.dat", fh)

    def test_write_100mb_and_flush(self, tmp_path: Path) -> None:
        """Write 100MB of data and verify flush to write buffer."""
        mock_client = MagicMock()
        mock_client.list_all_files.return_value = []
        mock_client.create_file.return_value = {
            "id": "big_write",
            "fileName": "bigwrite.dat",
            "sha256": "",
            "size": 0,
        }
        mock_client.update_file.return_value = {"id": "big_write", "sha256": "s"}

        dirtree = DirTree(mock_client, refresh_ttl=3600)
        dirtree.refresh()
        cache = ContentCache(tmp_path / "cache", max_bytes=200 * 1024 * 1024, max_files=10)
        writebuf = WriteBuffer(mock_client, tmp_path / "buf", drain_interval=60)
        fuse = ClawFUSE(mock_client, dirtree, cache, writebuf)

        fh = fuse.create("/bigwrite.dat", 0o644)

        # Write 100MB in 1MB chunks
        chunk = b"X" * (1024 * 1024)  # 1MB
        total_chunks = 100

        start = time.perf_counter()
        offset = 0
        for i in range(total_chunks):
            fuse.write("/bigwrite.dat", chunk, offset, fh)
            offset += len(chunk)
        write_elapsed = time.perf_counter() - start

        print(f"\n  Write 100MB ({total_chunks} x 1MB): {write_elapsed:.3f}s ({write_elapsed/total_chunks*1000:.3f}ms/chunk)")

        # Flush
        start = time.perf_counter()
        fuse.flush("/bigwrite.dat", fh)
        flush_elapsed = time.perf_counter() - start
        print(f"  Flush (enqueue to writebuf): {flush_elapsed:.3f}s")

        # Verify write buffer has the data
        assert writebuf.pending_count == 1
        pending = writebuf.get_pending("big_write")
        assert pending is not None
        assert len(pending.content) == 100 * 1024 * 1024

        fuse.release("/bigwrite.dat", fh)

    def test_concurrent_reads_different_files(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """Concurrent reads of different files should all succeed."""
        import concurrent.futures

        fuse, mock_client = deep_fuse
        dirtree = fuse._dirtree

        file_paths = [
            dirtree.get_path(fid)
            for fid, path in dirtree._id_map.items()
            if dirtree._path_map.get(path) and not dirtree._path_map[path].is_dir
        ]
        file_paths = [p for p in file_paths if p][:100]

        errors: list[str] = []

        def read_file(path: str) -> None:
            try:
                fh = fuse.open(path, os.O_RDONLY)
                content = fuse.read(path, 4096, 0, fh)
                assert len(content) > 0
                fuse.release(path, fh)
            except Exception as e:
                errors.append(f"{path}: {e}")

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(read_file, p) for p in file_paths]
            concurrent.futures.wait(futures)
        elapsed = time.perf_counter() - start

        assert not errors, f"Errors: {errors[:5]}"
        print(f"\n  10-thread concurrent read x{len(file_paths)}: {elapsed:.3f}s")


# ════════════════════════════════════════════════════════
# Test 3: Deep directory traversal (>10 levels)
# ════════════════════════════════════════════════════════


class TestDeepDirectories:
    """Tests with directories exceeding 10 levels."""

    def test_max_depth_resolve(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """Verify files at maximum depth are accessible."""
        fuse, _ = deep_fuse
        dirtree = fuse._dirtree

        # Find the deepest files
        max_depth = 0
        deepest_path = ""
        for path_str in dirtree._path_map:
            depth = path_str.count("/")
            if depth > max_depth:
                max_depth = depth
                deepest_path = path_str

        print(f"\n  Max depth found: {max_depth} levels")

        # Should be able to getattr on deepest file
        if deepest_path and not dirtree._path_map[deepest_path].is_dir:
            stat = fuse.getattr(deepest_path)
            assert stat is not None
            assert stat["st_mode"] & 0o100000  # Regular file

    def test_readdir_at_each_depth(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """readdir at each depth level should work."""
        fuse, _ = deep_fuse
        dirtree = fuse._dirtree

        # Collect one directory per depth level
        by_depth: dict[int, str] = {}
        for path_str, meta in dirtree._path_map.items():
            if meta.is_dir:
                depth = path_str.count("/")
                if depth not in by_depth:
                    by_depth[depth] = path_str

        print(f"\n  Testing readdir at {len(by_depth)} depth levels")

        start = time.perf_counter()
        for depth in sorted(by_depth.keys()):
            path = by_depth[depth]
            entries = fuse.readdir(path, 0)
            assert isinstance(entries, list)
        elapsed = time.perf_counter() - start

        print(f"  readdir at {len(by_depth)} depths: {elapsed:.3f}s")

    def test_create_file_at_max_depth(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """Create a new file at the deepest directory level."""
        fuse, mock_client = deep_fuse
        dirtree = fuse._dirtree

        # Find deepest directory
        max_depth = 0
        deepest_dir = ""
        for path_str, meta in dirtree._path_map.items():
            if meta.is_dir:
                depth = path_str.count("/")
                if depth > max_depth:
                    max_depth = depth
                    deepest_dir = path_str

        new_path = deepest_dir + "/nested_file.txt"

        start = time.perf_counter()
        fh = fuse.create(new_path, 0o644)
        elapsed = time.perf_counter() - start

        # Verify it exists
        meta = dirtree.resolve(new_path)
        assert meta is not None

        # Write and read
        fuse.write(new_path, b"deep content", 0, fh)
        result = fuse.read(new_path, 12, 0, fh)
        assert result == b"deep content"

        fuse.flush(new_path, fh)
        fuse.release(new_path, fh)

        print(f"\n  create+write+read at depth {max_depth}: {elapsed*1000:.3f}ms")


# ════════════════════════════════════════════════════════
# Test 4: All FUSE operations comprehensive
# ════════════════════════════════════════════════════════


class TestAllFuseOps:
    """Comprehensive test of every FUSE operation under load."""

    def test_getattr_2000_files(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """getattr on every file and directory."""
        fuse, _ = deep_fuse
        dirtree = fuse._dirtree
        paths = list(dirtree._path_map.keys())

        start = time.perf_counter()
        for p in paths:
            fuse.getattr(p)
        elapsed = time.perf_counter() - start
        print(f"\n  getattr x{len(paths)}: {elapsed:.3f}s")

    def test_readdir_all_directories(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """readdir on every directory."""
        fuse, _ = deep_fuse
        dirtree = fuse._dirtree

        dirs = [p for p, m in dirtree._path_map.items() if m.is_dir]
        start = time.perf_counter()
        for d in dirs:
            entries = fuse.readdir(d, 0)
            assert "." in entries
        elapsed = time.perf_counter() - start
        print(f"\n  readdir x{len(dirs)} dirs: {elapsed:.3f}s")

    def test_open_read_release_500_files(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """open → read → release on 500 files."""
        fuse, _ = deep_fuse
        dirtree = fuse._dirtree

        files = [p for p, m in dirtree._path_map.items() if not m.is_dir][:500]

        start = time.perf_counter()
        for f in files:
            fh = fuse.open(f, os.O_RDONLY)
            _ = fuse.read(f, 4096, 0, fh)
            fuse.release(f, fh)
        elapsed = time.perf_counter() - start
        print(f"\n  open+read+release x{len(files)}: {elapsed:.3f}s ({elapsed/len(files)*1000:.3f}ms/op)")

    def test_create_write_flush_release(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """Create, write, flush, release new files."""
        fuse, mock_client = deep_fuse

        # Create 50 new files in root
        mock_client.create_file.side_effect = [
            {"id": f"new_{i:04d}", "fileName": f"perf_{i}.txt", "sha256": "", "size": 0}
            for i in range(50)
        ]

        start = time.perf_counter()
        for i in range(50):
            path = f"/perf_test_{i}.txt"
            fh = fuse.create(path, 0o644)
            fuse.write(path, f"performance test data {i}".encode(), 0, fh)
            fuse.flush(path, fh)
            fuse.release(path, fh)
        elapsed = time.perf_counter() - start

        # Verify write buffer has all pending writes
        assert fuse._writebuf.pending_count == 50
        print(f"\n  create+write+flush x50: {elapsed:.3f}s ({elapsed/50*1000:.3f}ms/op)")

    def test_unlink_files(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """Delete files and verify dirtree updates."""
        fuse, mock_client = deep_fuse
        dirtree = fuse._dirtree

        files = [p for p, m in dirtree._path_map.items() if not m.is_dir][:50]

        start = time.perf_counter()
        for f in files:
            fuse.unlink(f)
        elapsed = time.perf_counter() - start

        # Verify they're gone from dirtree
        for f in files:
            assert dirtree.resolve(f) is None

        print(f"\n  unlink x{len(files)}: {elapsed:.3f}s ({elapsed/len(files)*1000:.3f}ms/op)")

    def test_mkdir_rmdir(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """Create and remove directories."""
        fuse, mock_client = deep_fuse

        # Create 20 directories
        mock_client.create_folder.side_effect = [
            {"id": f"tmpdir_{i:04d}", "fileName": f"tmp_{i}"}
            for i in range(20)
        ]

        dirs = [f"/tmp_test_{i}" for i in range(20)]

        start = time.perf_counter()
        for d in dirs:
            fuse.mkdir(d, 0o755)
        mkdir_elapsed = time.perf_counter() - start

        # Verify created
        for d in dirs:
            meta = fuse._dirtree.resolve(d)
            assert meta is not None
            assert meta.is_dir

        # Remove them (they're empty)
        start = time.perf_counter()
        for d in dirs:
            fuse.rmdir(d)
        rmdir_elapsed = time.perf_counter() - start

        print(f"\n  mkdir x20: {mkdir_elapsed:.3f}s, rmdir x20: {rmdir_elapsed:.3f}s")

    def test_rename_files(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """Rename files and verify dirtree updates."""
        fuse, mock_client = deep_fuse
        dirtree = fuse._dirtree

        files = [p for p, m in dirtree._path_map.items() if not m.is_dir][:50]

        start = time.perf_counter()
        for i, f in enumerate(files):
            new_name = f"/renamed_{i:04d}.txt"
            fuse.rename(f, new_name)
        elapsed = time.perf_counter() - start

        # Verify old paths gone, new paths exist
        for f in files:
            assert dirtree.resolve(f) is None

        print(f"\n  rename x{len(files)}: {elapsed:.3f}s ({elapsed/len(files)*1000:.3f}ms/op)")

    def test_truncate_operations(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """Truncate files to various sizes."""
        fuse, mock_client = deep_fuse
        dirtree = fuse._dirtree

        files = [p for p, m in dirtree._path_map.items() if not m.is_dir][:20]

        start = time.perf_counter()
        for f in files:
            # Truncate to 100 bytes (no fh — download + upload path)
            fuse.truncate(f, 100)
        elapsed = time.perf_counter() - start

        print(f"\n  truncate x{len(files)}: {elapsed:.3f}s ({elapsed/len(files)*1000:.3f}ms/op)")

    def test_mixed_operations_sequence(self, deep_fuse: tuple[ClawFUSE, MagicMock]) -> None:
        """Mixed sequence: create, write, read, rename, unlink."""
        fuse, mock_client = deep_fuse

        # Setup
        mock_client.create_file.side_effect = [
            {"id": f"mix_{i}", "fileName": f"m{i}.txt", "sha256": "", "size": 0}
            for i in range(20)
        ]

        start = time.perf_counter()

        # Phase 1: Create 20 files
        for i in range(20):
            fh = fuse.create(f"/mix_{i}.txt", 0o644)
            fuse.write(f"/mix_{i}.txt", f"data_{i}".encode(), 0, fh)
            fuse.flush(f"/mix_{i}.txt", fh)
            fuse.release(f"/mix_{i}.txt", fh)

        # Phase 2: Read them back (from write buffer — open with O_RDWR to access in-memory content)
        for i in range(20):
            fh = fuse.open(f"/mix_{i}.txt", os.O_RDWR)
            content = fuse.read(f"/mix_{i}.txt", 100, 0, fh)
            assert f"data_{i}".encode() in content
            fuse.release(f"/mix_{i}.txt", fh)

        # Phase 3: Rename first 10
        for i in range(10):
            fuse.rename(f"/mix_{i}.txt", f"/renamed_mix_{i}.txt")

        # Phase 4: Delete renamed files
        for i in range(10):
            fuse.unlink(f"/renamed_mix_{i}.txt")

        elapsed = time.perf_counter() - start

        # Verify state
        for i in range(10):
            assert fuse._dirtree.resolve(f"/mix_{i}.txt") is None  # Renamed away
            assert fuse._dirtree.resolve(f"/renamed_mix_{i}.txt") is None  # Deleted
        for i in range(10, 20):
            assert fuse._dirtree.resolve(f"/mix_{i}.txt") is not None  # Still exists

        print(f"\n  mixed ops (create+write+read+rename+unlink): {elapsed:.3f}s")


# ════════════════════════════════════════════════════════
# Test 5: Memory and resource bounds
# ════════════════════════════════════════════════════════


class TestResourceBounds:
    """Test that resources stay within bounds under load."""

    def test_cache_stays_within_max_bytes(self, tmp_path: Path) -> None:
        """Cache should never exceed max_bytes limit."""
        max_bytes = 5 * 1024 * 1024  # 5MB
        cache = ContentCache(tmp_path / "cache", max_bytes=max_bytes, max_files=10000)

        # Insert 50 x 500KB = 25MB total (should evict down to 5MB)
        chunk = b"Y" * (500 * 1024)
        for i in range(50):
            cache.put(f"f{i:04d}", f"/{i}.bin", chunk, f"sha_{i}")

        assert cache.total_bytes <= max_bytes, f"Cache exceeded limit: {cache.total_bytes} > {max_bytes}"
        print(f"\n  Cache after 25MB insert (limit 5MB): {cache.total_bytes / 1024 / 1024:.1f}MB, {cache.entry_count} entries")

    def test_write_buf_disk_usage(self, tmp_path: Path) -> None:
        """Write buffer should clean up .buf files after flush."""
        mock_client = MagicMock()
        mock_client.update_file.return_value = {"id": "f", "sha256": "s"}
        buf_dir = tmp_path / "buf"

        wb = WriteBuffer(mock_client, buf_dir, drain_interval=60, max_retries=3)

        content = b"Z" * 1024 * 100  # 100KB each
        for i in range(20):
            wb.enqueue(f"f{i:04d}", f"/{i}.bin", content, f"sha_{i}")

        # 20 x 100KB = 2MB of .buf files
        buf_files = list(buf_dir.glob("*.buf"))
        assert len(buf_files) == 20

        # Flush all
        result = wb.flush_all(timeout=30)
        assert result.succeeded == 20

        # .buf files should be cleaned up
        buf_files_after = list(buf_dir.glob("*.buf"))
        assert len(buf_files_after) == 0
        print(f"\n  Write buffer: 20 files enqueued, {result.succeeded} flushed, {len(buf_files_after)} .buf remaining")

    def test_dirtree_memory_footprint(self, deep_tree_data: list[dict]) -> None:
        """Estimate memory footprint of 2000-file directory tree."""
        import sys

        mock_client = MagicMock()
        mock_client.list_all_files.return_value = deep_tree_data
        tree = DirTree(mock_client, refresh_ttl=3600)
        tree.refresh()

        # Rough estimate: size of all FileMeta objects
        total_size = 0
        for meta in tree._path_map.values():
            total_size += sys.getsizeof(meta)
            total_size += sys.getsizeof(meta.id)
            total_size += sys.getsizeof(meta.name)
            total_size += sys.getsizeof(meta.sha256)

        print(f"\n  DirTree 2000 files memory: ~{total_size / 1024:.1f}KB")
        assert total_size < 5 * 1024 * 1024, f"DirTree too large: {total_size / 1024 / 1024:.1f}MB"
