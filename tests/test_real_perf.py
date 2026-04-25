"""Real Drive Kit API performance tests for ClawFUSE.

These tests hit the actual Drive Kit cloud API and measure real latency.
They require a valid token file at the path specified by CLAWFUSE_TOKEN_FILE
or default to test_token.json in the project root.

Usage:
    pytest tests/test_real_perf.py -v -s --tb=short

WARNING: These tests create/delete real files in your Drive Kit cloud storage.
         All test data is cleaned up after tests complete.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Skip all tests unless explicitly enabled
pytestmark = pytest.mark.realapi

# Resolve token file (absolute path)
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent

_env_token = os.environ.get("CLAWFUSE_TOKEN_FILE", "").strip()
if _env_token:
    _TOKEN_FILE = Path(_env_token).resolve()
elif (_PROJECT_ROOT / "test_token.json").is_file():
    _TOKEN_FILE = _PROJECT_ROOT / "test_token.json"
elif Path("D:/AI/drive_token.json").is_file():
    _TOKEN_FILE = Path("D:/AI/drive_token.json")
else:
    _TOKEN_FILE = Path()  # will fail
    pytestmark = pytest.mark.skip(reason="No token file found — set CLAWFUSE_TOKEN_FILE or place test_token.json in project root")


def _get_token() -> str:
    with open(_TOKEN_FILE, "r") as f:
        data = json.load(f)
    return data["access_token"]


# ── Test data prefix for cleanup ──
TEST_PREFIX = "clawfuse_perftest_"

# ── Import client modules ──
sys.path.insert(0, str(Path(__file__).parent.parent))

from clawfuse.cache import ContentCache
from clawfuse.client import DriveKitClient
from clawfuse.config import API_BASE, FOLDER_MIME, UPLOAD_BASE
from clawfuse.dirtree import DirTree, FileMeta
from clawfuse.fuse import ClawFUSE
from clawfuse.token import TokenManager
from clawfuse.writebuf import WriteBuffer


# ── Fixtures ──

@pytest.fixture(scope="module")
def token_mgr() -> TokenManager:
    return TokenManager(_TOKEN_FILE.resolve())


@pytest.fixture(scope="module")
def client(token_mgr: TokenManager) -> DriveKitClient:
    return DriveKitClient(token_mgr, timeout=60)


@pytest.fixture(scope="module")
def test_folder(client: DriveKitClient) -> dict:
    """Create a test root folder, yield its info, delete it after all tests."""
    ts = int(time.time())
    folder_name = f"{TEST_PREFIX}root_{ts}"
    result = client.create_folder(folder_name, parent_folder="applicationData")
    folder_id = result.get("id", "")
    print(f"\n[Setup] Created test folder: {folder_name} (id={folder_id})")

    yield {"id": folder_id, "name": folder_name}

    # Cleanup: delete everything under test folder
    try:
        _cleanup_folder(client, folder_id)
        client.delete_file(folder_id)
        print(f"\n[Teardown] Deleted test folder: {folder_id}")
    except Exception as e:
        print(f"\n[Teardown] Warning: cleanup error: {e}")


def _cleanup_folder(client: DriveKitClient, folder_id: str) -> None:
    """Recursively delete all contents under a folder."""
    items = client.list_all_files(root_folder=folder_id)
    # Delete files first (reverse order — deepest first not needed, Drive Kit handles it)
    for item in items:
        try:
            client.delete_file(item["id"])
        except Exception:
            pass


# ── Helpers ──

def _create_test_file(
    client: DriveKitClient,
    filename: str,
    content: bytes,
    parent_folder: str,
) -> dict:
    """Create a file via Drive Kit and return its metadata."""
    return client.create_file(
        filename=filename,
        content=content,
        parent_folder=parent_folder,
    )


def _create_test_folder(
    client: DriveKitClient,
    name: str,
    parent_folder: str,
) -> dict:
    """Create a folder via Drive Kit."""
    return client.create_folder(name, parent_folder)


# ═══════════════════════════════════════════════════════════════════════════
# Test Suite 1: Drive Kit API 原始性能
# ═══════════════════════════════════════════════════════════════════════════

class TestDriveKitAPIPerformance:
    """Measure raw Drive Kit API call latency."""

    def test_01_list_all_files_latency(self, client: DriveKitClient) -> None:
        """list_all_files (root) latency — baseline for DirTree refresh."""
        start = time.perf_counter()
        items = client.list_all_files(root_folder="applicationData")
        elapsed = time.perf_counter() - start

        print(f"\n[list_all_files] {len(items)} items in {elapsed:.3f}s")
        assert elapsed < 30.0, f"list_all_files took {elapsed:.1f}s (too slow)"
        # Store for reference
        self.__class__._total_cloud_files = len(items)

    def test_02_create_file_latency(self, client: DriveKitClient, test_folder: dict) -> None:
        """Create file (multipart upload) latency for different sizes."""
        parent_id = test_folder["id"]
        results: list[tuple[str, float]] = []

        for label, content in [
            ("1KB", b"x" * 1024),
            ("10KB", b"x" * 10240),
            ("100KB", b"x" * 102400),
            ("1MB", b"x" * (1024 * 1024)),
        ]:
            filename = f"{TEST_PREFIX}upload_{label.replace(' ', '_')}"
            start = time.perf_counter()
            result = client.create_file(filename, content, parent_folder=parent_id)
            elapsed = time.perf_counter() - start
            file_id = result.get("id", "")
            results.append((label, elapsed))
            print(f"  [create_file {label}] {elapsed:.3f}s (id={file_id[:16]}...)")

            # Register for cleanup
            test_folder.setdefault("_files", []).append(file_id)

        print(f"\n[create_file summary]")
        for label, t in results:
            print(f"  {label:>6}: {t:.3f}s")

        # All uploads should complete within 60s
        assert all(t < 60.0 for _, t in results), "Some uploads took > 60s"

    def test_03_download_file_latency(self, client: DriveKitClient, test_folder: dict) -> None:
        """Download file latency for different sizes."""
        parent_id = test_folder["id"]
        results: list[tuple[str, float, int]] = []

        for label, content in [
            ("1KB", b"x" * 1024),
            ("10KB", b"x" * 10240),
            ("100KB", b"x" * 102400),
            ("1MB", b"x" * (1024 * 1024)),
        ]:
            # Create first
            filename = f"{TEST_PREFIX}dl_{label.replace(' ', '_')}"
            create_result = client.create_file(filename, content, parent_folder=parent_id)
            file_id = create_result.get("id", "")

            # Download
            start = time.perf_counter()
            downloaded = client.download_file(file_id)
            elapsed = time.perf_counter() - start

            assert len(downloaded) == len(content)
            results.append((label, elapsed, len(content)))
            test_folder.setdefault("_files", []).append(file_id)

        print(f"\n[download_file summary]")
        for label, t, size in results:
            throughput = size / t / 1024 if t > 0 else 0
            print(f"  {label:>6}: {t:.3f}s ({throughput:.1f} KB/s)")

    def test_04_update_file_latency(self, client: DriveKitClient, test_folder: dict) -> None:
        """Update file content latency."""
        parent_id = test_folder["id"]

        # Create a file first
        create_result = client.create_file(
            f"{TEST_PREFIX}update_test", b"original content", parent_folder=parent_id,
        )
        file_id = create_result["id"]

        # Update with new content
        new_content = b"updated content " * 1000  # ~15KB
        start = time.perf_counter()
        client.update_file(file_id, new_content)
        elapsed = time.perf_counter() - start

        print(f"\n[update_file 15KB] {elapsed:.3f}s")

        # Verify
        downloaded = client.download_file(file_id)
        assert downloaded == new_content

        test_folder.setdefault("_files", []).append(file_id)
        assert elapsed < 30.0

    def test_05_delete_file_latency(self, client: DriveKitClient, test_folder: dict) -> None:
        """Delete file latency."""
        parent_id = test_folder["id"]
        results: list[float] = []

        for i in range(10):
            create_result = client.create_file(
                f"{TEST_PREFIX}del_{i:03d}", b"delete me", parent_folder=parent_id,
            )
            file_id = create_result["id"]

            start = time.perf_counter()
            client.delete_file(file_id)
            elapsed = time.perf_counter() - start
            results.append(elapsed)

        avg = sum(results) / len(results)
        print(f"\n[delete_file] avg {avg:.3f}s, min {min(results):.3f}s, max {max(results):.3f}s")
        assert avg < 5.0, f"Delete avg {avg:.1f}s too slow"

    def test_06_get_file_metadata_latency(self, client: DriveKitClient, test_folder: dict) -> None:
        """Get file metadata latency."""
        parent_id = test_folder["id"]

        # Create a file
        create_result = client.create_file(
            f"{TEST_PREFIX}meta_test", b"metadata test", parent_folder=parent_id,
        )
        file_id = create_result["id"]

        results: list[float] = []
        for _ in range(20):
            start = time.perf_counter()
            client.get_file(file_id)
            elapsed = time.perf_counter() - start
            results.append(elapsed)

        avg = sum(results) / len(results)
        p50 = sorted(results)[10]
        p95 = sorted(results)[19]

        print(f"\n[get_file metadata x20] avg {avg*1000:.1f}ms, p50 {p50*1000:.1f}ms, p95 {p95*1000:.1f}ms")
        test_folder.setdefault("_files", []).append(file_id)
        assert avg < 3.0, f"get_file avg {avg:.1f}s too slow"

    def test_07_create_folder_latency(self, client: DriveKitClient, test_folder: dict) -> None:
        """Create folder latency."""
        parent_id = test_folder["id"]
        results: list[float] = []

        for i in range(5):
            start = time.perf_counter()
            result = client.create_folder(f"{TEST_PREFIX}subdir_{i}", parent_id)
            elapsed = time.perf_counter() - start
            results.append(elapsed)
            test_folder.setdefault("_files", []).append(result["id"])

        avg = sum(results) / len(results)
        print(f"\n[create_folder x5] avg {avg:.3f}s, min {min(results):.3f}s, max {max(results):.3f}s")
        assert avg < 5.0

    def test_08_list_files_paginated(self, client: DriveKitClient, test_folder: dict) -> None:
        """List files with pagination latency."""
        parent_id = test_folder["id"]

        # Create 10 files to paginate through
        for i in range(10):
            client.create_file(
                f"{TEST_PREFIX}page_{i:03d}", f"content {i}".encode(), parent_folder=parent_id,
            )

        # List with small page size to force pagination
        start = time.perf_counter()
        result = client.list_files(parent_folder=parent_id, page_size=3)
        first_page_time = time.perf_counter() - start

        all_files = result.get("files", [])
        cursor = result.get("nextCursor")
        pages = 1

        while cursor:
            start = time.perf_counter()
            result = client.list_files(parent_folder=parent_id, page_size=3, cursor=cursor)
            elapsed = time.perf_counter() - start
            all_files.extend(result.get("files", []))
            cursor = result.get("nextCursor")
            pages += 1

        print(f"\n[list_files paginated] {len(all_files)} files in {pages} pages, first page: {first_page_time:.3f}s")


# ═══════════════════════════════════════════════════════════════════════════
# Test Suite 2: DirTree 真实加载性能
# ═══════════════════════════════════════════════════════════════════════════

class TestDirTreeRealPerformance:
    """Test DirTree loading and operations with real Drive Kit data."""

    def test_dirtree_refresh_real(self, client: DriveKitClient) -> None:
        """DirTree refresh with real Drive Kit data."""
        tree = DirTree(client, root_folder="applicationData", refresh_ttl=3600)

        start = time.perf_counter()
        tree.refresh()
        elapsed = time.perf_counter() - start

        print(f"\n[DirTree.refresh] {tree.file_count} items in {elapsed:.3f}s")
        assert tree.file_count > 0, "DirTree should have items"

        # Resolve a few paths
        start = time.perf_counter()
        for _ in range(100):
            tree.resolve("/")  # Always resolves
        elapsed_resolve = time.perf_counter() - start
        print(f"  [resolve / x100] {elapsed_resolve*1000:.2f}ms")

    def test_dirtree_list_dir_real(self, client: DriveKitClient) -> None:
        """DirTree list_dir with real data."""
        tree = DirTree(client, root_folder="applicationData", refresh_ttl=3600)
        tree.refresh()

        # List root directory
        start = time.perf_counter()
        for _ in range(20):
            entries = tree.list_dir("/")
        elapsed = time.perf_counter() - start

        print(f"\n[list_dir / x20] {elapsed*1000:.2f}ms, entries: {len(entries)}")

    def test_dirtree_deep_resolve(self, client: DriveKitClient, test_folder: dict) -> None:
        """DirTree resolve with deeply nested real folders."""
        parent_id = test_folder["id"]
        folder_ids = [parent_id]
        depth = 10

        # Create 10-level deep folder chain
        for i in range(depth):
            result = client.create_folder(f"L{i}", folder_ids[-1])
            folder_ids.append(result["id"])

        # Now refresh tree and try to resolve deep path
        tree = DirTree(client, root_folder="applicationData", refresh_ttl=3600)
        start = time.perf_counter()
        tree.refresh()
        elapsed_refresh = time.perf_counter() - start

        # Find the deepest folder
        deepest_id = folder_ids[-1]
        path = tree.get_path(deepest_id)
        print(f"\n[deep resolve] {depth} levels, refresh {elapsed_refresh:.3f}s, path: {path}")

        if path:
            start = time.perf_counter()
            for _ in range(100):
                tree.resolve(path)
            elapsed = time.perf_counter() - start
            print(f"  [resolve deep path x100] {elapsed*1000:.2f}ms ({elapsed/100*1000:.3f}ms/op)")


# ═══════════════════════════════════════════════════════════════════════════
# Test Suite 3: 完整 FUSE 流程性能（真实 API）
# ═══════════════════════════════════════════════════════════════════════════

class TestFUSEFullCycleReal:
    """Test complete FUSE read/write cycle with real Drive Kit API."""

    def test_read_cache_miss_then_hit(
        self, client: DriveKitClient, test_folder: dict, tmp_path: Path,
    ) -> None:
        """First read downloads from API (cache miss), second reads from disk cache."""
        parent_id = test_folder["id"]
        content = b"real API test content " * 500  # ~11KB

        # Create file on Drive Kit
        create_result = client.create_file(
            f"{TEST_PREFIX}read_test", content, parent_folder=parent_id,
        )
        file_id = create_result["id"]
        sha256 = hashlib.sha256(content).hexdigest()

        # Build a minimal FUSE ops instance
        tree = DirTree(client, root_folder="applicationData", refresh_ttl=3600)
        tree.refresh()

        cache = ContentCache(tmp_path / "cache", max_bytes=100 * 1024 * 1024, max_files=100)
        wb = WriteBuffer(client, tmp_path / "buf", drain_interval=60, max_retries=3)
        fuse_ops = ClawFUSE(client, tree, cache, wb, root_folder="applicationData")

        # Add file to tree manually (since we just created it)
        file_meta = FileMeta(
            id=file_id,
            name=f"{TEST_PREFIX}read_test",
            is_dir=False,
            size=len(content),
            sha256=sha256,
            parent_id=parent_id,
            modified_time="",
        )
        test_path = f"/{test_folder['name']}/{TEST_PREFIX}read_test"
        tree.add_entry(test_path, file_meta)

        # First read: cache miss → download
        fh = fuse_ops.open(test_path, os.O_RDONLY)
        start = time.perf_counter()
        data = fuse_ops.read(test_path, len(content), 0, fh)
        elapsed_miss = time.perf_counter() - start
        fuse_ops.release(test_path, fh)

        assert data == content
        print(f"\n[read cache miss] {elapsed_miss*1000:.1f}ms ({len(content)/1024:.1f}KB)")

        # Second read: cache hit
        fh = fuse_ops.open(test_path, os.O_RDONLY)
        start = time.perf_counter()
        data = fuse_ops.read(test_path, len(content), 0, fh)
        elapsed_hit = time.perf_counter() - start
        fuse_ops.release(test_path, fh)

        assert data == content
        print(f"[read cache hit]  {elapsed_hit*1000:.1f}ms ({len(content)/1024:.1f}KB)")
        print(f"[speedup]         {elapsed_miss/elapsed_hit:.1f}x faster")

    def test_write_flush_real(
        self, client: DriveKitClient, test_folder: dict, tmp_path: Path,
    ) -> None:
        """Write → flush → verify content on Drive Kit."""
        parent_id = test_folder["id"]

        tree = DirTree(client, root_folder="applicationData", refresh_ttl=3600)
        tree.refresh()

        cache = ContentCache(tmp_path / "cache", max_bytes=100 * 1024 * 1024, max_files=100)
        wb = WriteBuffer(client, tmp_path / "buf", drain_interval=60, max_retries=3)
        fuse_ops = ClawFUSE(client, tree, cache, wb, root_folder="applicationData")

        # Create file via FUSE
        test_path = f"/{test_folder['name']}/{TEST_PREFIX}write_test"
        start = time.perf_counter()
        fh = fuse_ops.create(test_path, 0o644)
        elapsed_create = time.perf_counter() - start

        # Write content
        content = b"written via FUSE " * 1000  # ~15KB
        start = time.perf_counter()
        fuse_ops.write(test_path, content, 0, fh)
        elapsed_write = time.perf_counter() - start

        # Flush (triggers upload via write buffer)
        start = time.perf_counter()
        fuse_ops.flush(test_path, fh)
        elapsed_flush = time.perf_counter() - start
        fuse_ops.release(test_path, fh)

        # Drain write buffer
        start = time.perf_counter()
        wb.flush_all(timeout=30)
        elapsed_drain = time.perf_counter() - start

        print(f"\n[write cycle]")
        print(f"  create:  {elapsed_create*1000:.1f}ms")
        print(f"  write:   {elapsed_write*1000:.1f}ms ({len(content)/1024:.1f}KB)")
        print(f"  flush:   {elapsed_flush*1000:.1f}ms")
        print(f"  drain:   {elapsed_drain*1000:.1f}ms (upload to Drive Kit)")
        print(f"  total:   {(elapsed_create+elapsed_write+elapsed_flush+elapsed_drain)*1000:.1f}ms")

        # Verify: download directly from Drive Kit
        file_id = tree.resolve(test_path)
        assert file_id is not None
        downloaded = client.download_file(file_id.id)
        assert downloaded == content, f"Content mismatch: {len(downloaded)} != {len(content)}"
        print(f"  verify:  content matches ({len(content)} bytes)")

    def test_mkdir_rmdir_real(
        self, client: DriveKitClient, test_folder: dict, tmp_path: Path,
    ) -> None:
        """mkdir and rmdir with real Drive Kit API."""
        parent_id = test_folder["id"]

        tree = DirTree(client, root_folder="applicationData", refresh_ttl=3600)
        tree.refresh()

        cache = ContentCache(tmp_path / "cache", max_bytes=100 * 1024 * 1024, max_files=100)
        wb = WriteBuffer(client, tmp_path / "buf", drain_interval=60, max_retries=3)
        fuse_ops = ClawFUSE(client, tree, cache, wb, root_folder="applicationData")

        test_dir = f"/{test_folder['name']}/{TEST_PREFIX}test_dir"

        # mkdir
        start = time.perf_counter()
        fuse_ops.mkdir(test_dir, 0o755)
        elapsed_mkdir = time.perf_counter() - start

        # Verify it exists
        meta = tree.resolve(test_dir)
        assert meta is not None and meta.is_dir

        # rmdir
        start = time.perf_counter()
        fuse_ops.rmdir(test_dir)
        elapsed_rmdir = time.perf_counter() - start

        # Verify it's gone
        meta = tree.resolve(test_dir)
        assert meta is None

        print(f"\n[mkdir] {elapsed_mkdir*1000:.1f}ms")
        print(f"[rmdir] {elapsed_rmdir*1000:.1f}ms")

    def test_unlink_real(
        self, client: DriveKitClient, test_folder: dict, tmp_path: Path,
    ) -> None:
        """unlink with real Drive Kit API."""
        parent_id = test_folder["id"]

        tree = DirTree(client, root_folder="applicationData", refresh_ttl=3600)
        tree.refresh()

        cache = ContentCache(tmp_path / "cache", max_bytes=100 * 1024 * 1024, max_files=100)
        wb = WriteBuffer(client, tmp_path / "buf", drain_interval=60, max_retries=3)
        fuse_ops = ClawFUSE(client, tree, cache, wb, root_folder="applicationData")

        # Create file first
        test_path = f"/{test_folder['name']}/{TEST_PREFIX}unlink_test"
        fh = fuse_ops.create(test_path, 0o644)
        fuse_ops.write(test_path, b"to be deleted", 0, fh)
        fuse_ops.flush(test_path, fh)
        fuse_ops.release(test_path, fh)
        wb.flush_all(timeout=30)

        # Verify exists
        meta = tree.resolve(test_path)
        assert meta is not None

        # Unlink
        start = time.perf_counter()
        fuse_ops.unlink(test_path)
        elapsed = time.perf_counter() - start

        # Verify gone
        meta = tree.resolve(test_path)
        assert meta is None

        print(f"\n[unlink] {elapsed*1000:.1f}ms")

    def test_rename_real(
        self, client: DriveKitClient, test_folder: dict, tmp_path: Path,
    ) -> None:
        """rename with real Drive Kit API."""
        parent_id = test_folder["id"]

        tree = DirTree(client, root_folder="applicationData", refresh_ttl=3600)
        tree.refresh()

        cache = ContentCache(tmp_path / "cache", max_bytes=100 * 1024 * 1024, max_files=100)
        wb = WriteBuffer(client, tmp_path / "buf", drain_interval=60, max_retries=3)
        fuse_ops = ClawFUSE(client, tree, cache, wb, root_folder="applicationData")

        # Create file
        old_path = f"/{test_folder['name']}/{TEST_PREFIX}old_name"
        fh = fuse_ops.create(old_path, 0o644)
        fuse_ops.write(old_path, b"renamed content", 0, fh)
        fuse_ops.flush(old_path, fh)
        fuse_ops.release(old_path, fh)
        wb.flush_all(timeout=30)

        # Rename
        new_path = f"/{test_folder['name']}/{TEST_PREFIX}new_name"
        start = time.perf_counter()
        fuse_ops.rename(old_path, new_path)
        elapsed = time.perf_counter() - start

        # Verify
        assert tree.resolve(old_path) is None
        assert tree.resolve(new_path) is not None

        print(f"\n[rename] {elapsed*1000:.1f}ms")


# ═══════════════════════════════════════════════════════════════════════════
# Test Suite 4: 大文件上传下载
# ═══════════════════════════════════════════════════════════════════════════

class TestLargeFilePerformance:
    """Test large file upload and download performance."""

    def test_large_file_upload_download(self, client: DriveKitClient, test_folder: dict) -> None:
        """Upload and download 5MB file via multipart upload."""
        parent_id = test_folder["id"]
        size = 5 * 1024 * 1024  # 5MB
        content = os.urandom(size)

        # Upload
        start = time.perf_counter()
        result = client.create_file(
            f"{TEST_PREFIX}large_5mb", content, parent_folder=parent_id,
        )
        elapsed_upload = time.perf_counter() - start
        file_id = result["id"]

        # Download
        start = time.perf_counter()
        downloaded = client.download_file(file_id)
        elapsed_download = time.perf_counter() - start

        # Verify
        assert len(downloaded) == size
        assert downloaded == content

        upload_throughput = size / elapsed_upload / 1024 / 1024  # MB/s
        download_throughput = size / elapsed_download / 1024 / 1024  # MB/s

        print(f"\n[5MB file]")
        print(f"  upload:   {elapsed_upload:.3f}s ({upload_throughput:.2f} MB/s)")
        print(f"  download: {elapsed_download:.3f}s ({download_throughput:.2f} MB/s)")
        print(f"  verify:   content matches ({size/1024/1024:.1f}MB)")

        assert elapsed_upload < 120.0, f"Upload too slow: {elapsed_upload:.1f}s"
        assert elapsed_download < 120.0, f"Download too slow: {elapsed_download:.1f}s"

    def test_large_file_update(self, client: DriveKitClient, test_folder: dict) -> None:
        """Update (overwrite) a large file."""
        parent_id = test_folder["id"]

        # Create first
        content_v1 = b"v1 " * (1024 * 512)  # ~1.5MB
        result = client.create_file(
            f"{TEST_PREFIX}update_large", content_v1, parent_folder=parent_id,
        )
        file_id = result["id"]

        # Update with v2
        content_v2 = os.urandom(2 * 1024 * 1024)  # 2MB random
        start = time.perf_counter()
        client.update_file(file_id, content_v2)
        elapsed = time.perf_counter() - start

        # Verify
        downloaded = client.download_file(file_id)
        assert downloaded == content_v2

        throughput = len(content_v2) / elapsed / 1024 / 1024
        print(f"\n[update 2MB] {elapsed:.3f}s ({throughput:.2f} MB/s)")


# ═══════════════════════════════════════════════════════════════════════════
# Test Suite 5: 并发测试
# ═══════════════════════════════════════════════════════════════════════════

class TestConcurrencyReal:
    """Test concurrent API operations."""

    def test_concurrent_reads(
        self, client: DriveKitClient, test_folder: dict,
    ) -> None:
        """Concurrent downloads from Drive Kit."""
        import concurrent.futures

        parent_id = test_folder["id"]

        # Create 5 files
        file_ids: list[str] = []
        for i in range(5):
            content = f"concurrent test {i} ".encode() * 500
            result = client.create_file(
                f"{TEST_PREFIX}concurrent_{i}", content, parent_folder=parent_id,
            )
            file_ids.append(result["id"])

        # Download concurrently
        errors: list[str] = []
        download_times: list[float] = []

        def download_one(fid: str) -> None:
            try:
                start = time.perf_counter()
                data = client.download_file(fid)
                elapsed = time.perf_counter() - start
                download_times.append(elapsed)
                assert len(data) > 0
            except Exception as e:
                errors.append(str(e))

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(download_one, fid) for fid in file_ids * 3]  # 15 downloads
            concurrent.futures.wait(futures)
        elapsed_total = time.perf_counter() - start

        assert not errors, f"Errors: {errors}"
        avg = sum(download_times) / len(download_times) if download_times else 0
        print(f"\n[concurrent reads] 5 threads x 3 rounds = 15 downloads")
        print(f"  total:   {elapsed_total:.3f}s")
        print(f"  avg/op:  {avg*1000:.1f}ms")

    def test_concurrent_mixed_ops(
        self, client: DriveKitClient, test_folder: dict,
    ) -> None:
        """Mix of concurrent creates and downloads."""
        import concurrent.futures

        parent_id = test_folder["id"]
        errors: list[str] = []

        def create_and_download(idx: int) -> None:
            try:
                content = f"mixed {idx} ".encode() * 200
                result = client.create_file(
                    f"{TEST_PREFIX}mixed_{idx}", content, parent_folder=parent_id,
                )
                fid = result["id"]
                downloaded = client.download_file(fid)
                assert downloaded == content
            except Exception as e:
                errors.append(str(e))

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(create_and_download, i) for i in range(10)]
            concurrent.futures.wait(futures)
        elapsed = time.perf_counter() - start

        assert not errors, f"Errors: {errors}"
        print(f"\n[mixed create+download x10] {elapsed:.3f}s ({elapsed/10*1000:.1f}ms/op)")


# ═══════════════════════════════════════════════════════════════════════════
# Test Suite 6: 端到端吞吐汇总
# ═══════════════════════════════════════════════════════════════════════════

class TestEndToEndSummary:
    """Aggregate throughput measurements."""

    def test_e2e_summary(self, client: DriveKitClient, test_folder: dict) -> None:
        """Print end-to-end performance summary."""
        parent_id = test_folder["id"]
        results: dict[str, float] = {}

        # 1. Create 1MB file
        content_1mb = b"x" * (1024 * 1024)
        start = time.perf_counter()
        r = client.create_file(f"{TEST_PREFIX}e2e_1mb", content_1mb, parent_folder=parent_id)
        results["create_1mb"] = time.perf_counter() - start
        fid = r["id"]

        # 2. Download 1MB
        start = time.perf_counter()
        client.download_file(fid)
        results["download_1mb"] = time.perf_counter() - start

        # 3. Update 1MB
        start = time.perf_counter()
        client.update_file(fid, content_1mb)
        results["update_1mb"] = time.perf_counter() - start

        # 4. Get metadata
        start = time.perf_counter()
        for _ in range(10):
            client.get_file(fid)
        results["get_meta_10x"] = time.perf_counter() - start

        # 5. List files
        start = time.perf_counter()
        client.list_files(parent_folder=parent_id)
        results["list_dir"] = time.perf_counter() - start

        # 6. Delete
        start = time.perf_counter()
        client.delete_file(fid)
        results["delete"] = time.perf_counter() - start

        # Print summary table
        print(f"\n{'='*60}")
        print(f"  ClawFUSE Real API Performance Summary")
        print(f"{'='*60}")
        print(f"  {'Operation':<25} {'Time':>10} {'Throughput':>15}")
        print(f"  {'-'*25} {'-'*10} {'-'*15}")

        ops = [
            ("create 1MB", results["create_1mb"], 1.0 / results["create_1mb"]),
            ("download 1MB", results["download_1mb"], 1.0 / results["download_1mb"]),
            ("update 1MB", results["update_1mb"], 1.0 / results["update_1mb"]),
            ("get metadata x10", results["get_meta_10x"], None),
            ("list directory", results["list_dir"], None),
            ("delete file", results["delete"], None),
        ]

        for name, t, tp in ops:
            tp_str = f"{tp:.2f} MB/s" if tp else "-"
            print(f"  {name:<25} {t:>9.3f}s {tp_str:>15}")
        print(f"{'='*60}")
