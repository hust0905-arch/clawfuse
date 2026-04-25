"""Tests for cache module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawfuse.cache import ContentCache
from clawfuse.exceptions import CacheError


def test_put_and_get(tmp_path: Path) -> None:
    """put + get roundtrip."""
    cache = ContentCache(tmp_path / "cache", max_bytes=1024 * 1024, max_files=100)
    cache.put("file1", "/data/test.txt", b"hello world", "sha256_abc")
    result = cache.get("file1")
    assert result == b"hello world"


def test_get_miss(tmp_path: Path) -> None:
    """get returns None on cache miss."""
    cache = ContentCache(tmp_path / "cache", max_bytes=1024 * 1024, max_files=100)
    assert cache.get("nonexistent") is None


def test_contains(tmp_path: Path) -> None:
    """contains checks index."""
    cache = ContentCache(tmp_path / "cache", max_bytes=1024 * 1024, max_files=100)
    assert not cache.contains("file1")
    cache.put("file1", "/test.txt", b"data", "sha")
    assert cache.contains("file1")


def test_invalidate(tmp_path: Path) -> None:
    """invalidate removes entry."""
    cache = ContentCache(tmp_path / "cache", max_bytes=1024 * 1024, max_files=100)
    cache.put("file1", "/test.txt", b"data", "sha")
    cache.invalidate("file1")
    assert not cache.contains("file1")
    assert cache.get("file1") is None


def test_total_bytes_tracking(tmp_path: Path) -> None:
    """total_bytes tracks cache size."""
    cache = ContentCache(tmp_path / "cache", max_bytes=1024 * 1024, max_files=100)
    cache.put("file1", "/a.txt", b"aaa", "sha1")
    cache.put("file2", "/b.txt", b"bbbbb", "sha2")
    assert cache.total_bytes == 8  # 3 + 5


def test_lru_eviction_on_bytes(tmp_path: Path) -> None:
    """LRU evicts oldest entries when exceeding max_bytes."""
    cache = ContentCache(tmp_path / "cache", max_bytes=10, max_files=100)
    cache.put("old", "/old.txt", b"12345", "sha_old")  # 5 bytes
    cache.put("new", "/new.txt", b"67890", "sha_new")  # 5 bytes, total=10

    # Both should exist
    assert cache.contains("old")
    assert cache.contains("new")

    # Add one more, triggers eviction of "old" (LRU)
    cache.put("newest", "/newest.txt", b"abcde", "sha_newest")  # 5 bytes

    assert not cache.contains("old")  # Evicted
    assert cache.contains("new")
    assert cache.contains("newest")


def test_lru_eviction_on_files(tmp_path: Path) -> None:
    """LRU evicts when exceeding max_files."""
    cache = ContentCache(tmp_path / "cache", max_bytes=1024 * 1024, max_files=2)
    cache.put("f1", "/1.txt", b"a", "s1")
    cache.put("f2", "/2.txt", b"b", "s2")
    cache.put("f3", "/3.txt", b"c", "s3")  # Triggers eviction of f1

    assert not cache.contains("f1")
    assert cache.contains("f2")
    assert cache.contains("f3")


def test_lru_access_updates_order(tmp_path: Path) -> None:
    """Accessing a cache entry moves it to MRU position."""
    cache = ContentCache(tmp_path / "cache", max_bytes=10, max_files=100)
    cache.put("old", "/old.txt", b"12345", "sha_old")
    cache.put("mid", "/mid.txt", b"67890", "sha_mid")

    # Access "old" to make it MRU
    cache.get("old")

    # Add new entry, should evict "mid" (now LRU) instead of "old"
    cache.put("new", "/new.txt", b"abcde", "sha_new")

    assert cache.contains("old")  # Still there (was accessed recently)
    assert not cache.contains("mid")  # Evicted (was LRU)


def test_overwrite_existing_entry(tmp_path: Path) -> None:
    """put on existing file_id replaces the entry."""
    cache = ContentCache(tmp_path / "cache", max_bytes=1024 * 1024, max_files=100)
    cache.put("f1", "/test.txt", b"old content", "sha_old")
    cache.put("f1", "/test.txt", b"new content longer", "sha_new")

    result = cache.get("f1")
    assert result == b"new content longer"
    assert cache.total_bytes == len(b"new content longer")


def test_restore_from_disk(tmp_path: Path) -> None:
    """Cache rebuilds index from .meta files on restart."""
    cache_dir = tmp_path / "cache"
    cache = ContentCache(cache_dir, max_bytes=1024 * 1024, max_files=100)
    cache.put("f1", "/test.txt", b"cached data", "sha_f1")
    cache.put("f2", "/other.txt", b"more data here", "sha_f2")

    # Create a new cache instance (simulates restart)
    cache2 = ContentCache(cache_dir, max_bytes=1024 * 1024, max_files=100)
    assert cache2.contains("f1")
    assert cache2.contains("f2")
    assert cache2.get("f1") == b"cached data"
    assert cache2.total_bytes == len(b"cached data") + len(b"more data here")


def test_entry_count(tmp_path: Path) -> None:
    """entry_count is accurate."""
    cache = ContentCache(tmp_path / "cache", max_bytes=1024 * 1024, max_files=100)
    assert cache.entry_count == 0
    cache.put("f1", "/1.txt", b"a", "s1")
    assert cache.entry_count == 1
    cache.put("f2", "/2.txt", b"b", "s2")
    assert cache.entry_count == 2
    cache.invalidate("f1")
    assert cache.entry_count == 1
