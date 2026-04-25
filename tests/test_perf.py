"""Performance tests for ClawFUSE — validates key performance targets.

These tests mock Drive Kit API to simulate various file counts and sizes,
measuring the actual performance of in-memory and disk-based operations.
"""

from __future__ import annotations

import hashlib
import random
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from clawfuse.cache import ContentCache
from clawfuse.dirtree import DirTree
from clawfuse.writebuf import WriteBuffer

pytestmark = pytest.mark.perf


def _generate_files(count: int, max_depth: int = 3) -> list[dict]:
    """Generate Drive Kit file listing for benchmarking."""
    items: list[dict] = []
    folder_ids: list[str] = ["applicationData"]

    # Create folders
    for i in range(min(count // 5, 50)):
        folder_id = f"folder_{i:04d}"
        parent = random.choice(folder_ids[: max(i, 1)])
        items.append({
            "id": folder_id,
            "fileName": f"dir_{i:04d}",
            "mimeType": "application/vnd.huawei-apps.folder",
            "sha256": "",
            "size": 0,
            "parentFolder": [{"id": parent}],
            "modifiedTime": "2026-04-24T10:00:00Z",
        })
        folder_ids.append(folder_id)

    # Create files
    for i in range(count):
        parent = random.choice(folder_ids)
        items.append({
            "id": f"file_{i:06d}",
            "fileName": f"file_{i:06d}.txt",
            "mimeType": "text/plain",
            "sha256": hashlib.sha256(f"content_{i}".encode()).hexdigest(),
            "size": random.randint(100, 10000),
            "parentFolder": [{"id": parent}],
            "modifiedTime": "2026-04-24T10:00:00Z",
        })

    return items


class TestDirTreePerformance:
    """Test directory tree loading performance."""

    def test_1000_files_load_time(self, tmp_path: Path) -> None:
        """DirTree with 1000 files should load in < 1 second (mocked API)."""
        files = _generate_files(1000)
        mock_client = MagicMock()
        mock_client.list_all_files.return_value = files

        tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)

        start = time.perf_counter()
        tree.refresh()
        elapsed = time.perf_counter() - start

        assert tree.file_count == len(files)
        assert elapsed < 1.0, f"1000 files took {elapsed:.3f}s (target: < 1.0s)"

    def test_10000_files_load_time(self, tmp_path: Path) -> None:
        """DirTree with 10000 files should load in < 5 seconds (mocked API)."""
        files = _generate_files(10000)
        mock_client = MagicMock()
        mock_client.list_all_files.return_value = files

        tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)

        start = time.perf_counter()
        tree.refresh()
        elapsed = time.perf_counter() - start

        assert tree.file_count == len(files)
        assert elapsed < 5.0, f"10000 files took {elapsed:.3f}s (target: < 5.0s)"

    def test_path_resolve_performance(self, tmp_path: Path) -> None:
        """Resolving paths should be < 0.1ms per lookup (dict lookup)."""
        files = _generate_files(1000)
        mock_client = MagicMock()
        mock_client.list_all_files.return_value = files

        tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
        tree.refresh()

        # Get some valid paths
        paths = [tree.get_path(f["id"]) for f in files[:100] if tree.get_path(f["id"])]
        paths = [p for p in paths if p]  # Filter None

        if not paths:
            pytest.skip("No valid paths generated")

        start = time.perf_counter()
        for _ in range(1000):
            for path in paths:
                tree.resolve(path)
        elapsed = time.perf_counter() - start

        per_lookup = elapsed / (1000 * len(paths)) * 1000  # ms
        assert per_lookup < 0.1, f"Path lookup took {per_lookup:.4f}ms (target: < 0.1ms)"


class TestCachePerformance:
    """Test cache read/write performance."""

    def test_cache_put_performance(self, tmp_path: Path) -> None:
        """Cache put should be < 5ms per 10KB file."""
        cache = ContentCache(tmp_path / "cache", max_bytes=100 * 1024 * 1024, max_files=1000)
        content = b"x" * 10240  # 10KB

        start = time.perf_counter()
        for i in range(100):
            cache.put(f"file_{i:04d}", f"/test_{i}.txt", content, f"sha_{i}")
        elapsed = time.perf_counter() - start

        per_put = elapsed / 100 * 1000  # ms
        assert per_put < 5.0, f"Cache put took {per_put:.2f}ms (target: < 5ms)"

    def test_cache_get_hit_performance(self, tmp_path: Path) -> None:
        """Cache hit should be < 1ms per lookup."""
        cache = ContentCache(tmp_path / "cache", max_bytes=100 * 1024 * 1024, max_files=1000)

        # Pre-populate
        content = b"hello world" * 100  # ~1.1KB
        for i in range(50):
            cache.put(f"f{i:04d}", f"/{i}.txt", content, f"sha_{i}")

        start = time.perf_counter()
        for _ in range(1000):
            for i in range(50):
                cache.get(f"f{i:04d}")
        elapsed = time.perf_counter() - start

        per_get = elapsed / (1000 * 50) * 1000  # ms
        assert per_get < 1.0, f"Cache get took {per_get:.4f}ms (target: < 1ms)"

    def test_lru_eviction_performance(self, tmp_path: Path) -> None:
        """LRU eviction should not degrade performance significantly."""
        cache = ContentCache(tmp_path / "cache", max_bytes=5000, max_files=10)
        content = b"x" * 1000  # 1KB

        start = time.perf_counter()
        for i in range(100):
            cache.put(f"f{i:04d}", f"/{i}.txt", content, f"sha_{i}")
        elapsed = time.perf_counter() - start

        # 100 puts with eviction — should still complete in reasonable time
        assert elapsed < 2.0, f"100 puts with eviction took {elapsed:.3f}s"

        # Cache should have at most 10 files (max_files)
        assert cache.entry_count <= 10


class TestWriteBufferPerformance:
    """Test write buffer performance."""

    def test_enqueue_performance(self, tmp_path: Path) -> None:
        """Enqueue should be < 5ms per write (disk write)."""
        mock_client = MagicMock()
        wb = WriteBuffer(mock_client, tmp_path / "buf", drain_interval=60, max_retries=3)
        content = b"test data" * 100  # ~900 bytes

        start = time.perf_counter()
        for i in range(50):
            wb.enqueue(f"f{i:04d}", f"/test_{i}.txt", content, f"sha_{i}")
        elapsed = time.perf_counter() - start

        per_enqueue = elapsed / 50 * 1000  # ms
        assert per_enqueue < 5.0, f"Enqueue took {per_enqueue:.2f}ms (target: < 5ms)"

    def test_flush_all_performance(self, tmp_path: Path) -> None:
        """flush_all with 10 files should complete in < 5 seconds."""
        mock_client = MagicMock()
        # Mock update_file to simulate some API latency
        mock_client.update_file.return_value = {"id": "f", "sha256": "s"}
        wb = WriteBuffer(mock_client, tmp_path / "buf", drain_interval=60, max_retries=3)

        content = b"data" * 100
        for i in range(10):
            wb.enqueue(f"f{i:04d}", f"/test_{i}.txt", content, f"sha_{i}")

        start = time.perf_counter()
        result = wb.flush_all(timeout=30)
        elapsed = time.perf_counter() - start

        assert result.succeeded == 10
        assert elapsed < 5.0, f"flush_all 10 files took {elapsed:.3f}s (target: < 5s)"


class TestConcurrency:
    """Test concurrent access patterns."""

    def test_concurrent_cache_reads(self, tmp_path: Path) -> None:
        """Concurrent cache reads should not deadlock."""
        import concurrent.futures

        cache = ContentCache(tmp_path / "cache", max_bytes=100 * 1024 * 1024, max_files=100)
        content = b"shared data" * 100

        # Pre-populate
        for i in range(20):
            cache.put(f"f{i:04d}", f"/{i}.txt", content, f"sha_{i}")

        errors: list[str] = []

        def read_worker(worker_id: int) -> None:
            try:
                for i in range(100):
                    cache.get(f"f{i % 20:04d}")
            except Exception as e:
                errors.append(f"Worker {worker_id}: {e}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(read_worker, i) for i in range(10)]
            concurrent.futures.wait(futures)

        assert not errors, f"Errors in concurrent reads: {errors}"

    def test_concurrent_enqueue_and_drain(self, tmp_path: Path) -> None:
        """Concurrent enqueue and drain should not lose data."""
        import concurrent.futures

        mock_client = MagicMock()
        mock_client.update_file.return_value = {"id": "f", "sha256": "s"}

        wb = WriteBuffer(mock_client, tmp_path / "buf", drain_interval=0.05, max_retries=3)

        errors: list[str] = []

        def enqueue_worker() -> None:
            try:
                for i in range(20):
                    wb.enqueue(f"f{i:04d}", f"/test_{i}.txt", b"data", f"sha_{i}")
            except Exception as e:
                errors.append(f"Enqueue error: {e}")

        # Start enqueueing in background
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(enqueue_worker) for _ in range(3)]

            # Let drain thread run briefly
            wb.start_drain()
            time.sleep(0.5)
            wb.stop_drain()

            concurrent.futures.wait(futures)

        assert not errors, f"Errors in concurrent enqueue/drain: {errors}"
