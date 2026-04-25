"""Tests for lifecycle module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from clawfuse.config import Config
from clawfuse.lifecycle import LifecycleManager, MountResult, SyncResult


def _make_config(tmp_path: Path, **overrides) -> Config:
    """Helper to create a Config with sensible test defaults."""
    defaults = {
        "token_file": tmp_path / "token",
        "token_string": "",
        "cloud_folder": "applicationData",
        "mount_point": str(tmp_path / "mnt"),
        "root_folder": "applicationData",
        "cache_dir": tmp_path / "cache",
        "cache_max_bytes": 1024,
        "cache_max_files": 10,
        "write_buf_dir": tmp_path / "buf",
        "drain_interval": 1.0,
        "drain_max_retries": 3,
        "tree_refresh_ttl": 3600,
        "list_page_size": 200,
        "http_timeout": 10,
        "log_level": "DEBUG",
    }
    defaults.update(overrides)
    return Config(**defaults)


def test_pre_start_success(config: Config) -> None:
    """pre_start succeeds with valid config and mocked Drive Kit."""
    config.ensure_dirs()
    lifecycle = LifecycleManager(config)

    # Mock DriveKitClient.list_all_files to avoid real API calls
    with patch("clawfuse.lifecycle.DriveKitClient") as MockClient:
        mock_client = MagicMock()
        mock_client.list_all_files.return_value = []
        mock_client.list_files.return_value = {"files": []}
        MockClient.return_value = mock_client

        result = lifecycle.pre_start()
        assert result.success
        assert result.file_count == 0
        assert result.load_time_seconds > 0
        assert lifecycle.is_mounted


def test_pre_start_with_string_token(tmp_path: Path) -> None:
    """pre_start works with token_string (JSON config mode)."""
    cfg = _make_config(tmp_path, token_file=None, token_string="test_token_abc")
    cfg.ensure_dirs()
    lifecycle = LifecycleManager(cfg)

    with patch("clawfuse.lifecycle.DriveKitClient") as MockClient:
        mock_client = MagicMock()
        mock_client.list_all_files.return_value = []
        mock_client.list_files.return_value = {"files": []}
        MockClient.return_value = mock_client

        result = lifecycle.pre_start()
        assert result.success


def test_pre_start_no_token_file(tmp_path: Path) -> None:
    """pre_start fails when no token is configured."""
    cfg = _make_config(
        tmp_path,
        token_file=tmp_path / "nonexistent_token",
    )
    lifecycle = LifecycleManager(cfg)
    result = lifecycle.pre_start()
    assert not result.success
    assert "not found" in result.error or "empty" in result.error or "Token" in result.error


def test_pre_destroy_no_pending(config: Config) -> None:
    """pre_destroy with no pending writes succeeds."""
    config.ensure_dirs()
    lifecycle = LifecycleManager(config)

    with patch("clawfuse.lifecycle.DriveKitClient") as MockClient:
        mock_client = MagicMock()
        mock_client.list_all_files.return_value = []
        mock_client.list_files.return_value = {"files": []}
        MockClient.return_value = mock_client
        lifecycle.pre_start()

    result = lifecycle.pre_destroy()
    assert result.files_synced == 0
    assert result.files_failed == 0
    assert not lifecycle.is_mounted


def test_status(config: Config) -> None:
    """status returns correct state after pre_start."""
    config.ensure_dirs()
    lifecycle = LifecycleManager(config)

    with patch("clawfuse.lifecycle.DriveKitClient") as MockClient:
        mock_client = MagicMock()
        mock_client.list_all_files.return_value = []
        mock_client.list_files.return_value = {"files": []}
        MockClient.return_value = mock_client
        lifecycle.pre_start()

    status = lifecycle.status()
    assert status.mounted
    assert status.file_count == 0
    assert status.cache_entries == 0
    assert status.pending_writes == 0
    assert status.uptime_seconds >= 0


def test_status_before_start(tmp_path: Path) -> None:
    """status before pre_start shows unmounted."""
    token_file = tmp_path / "token"
    token_file.write_text("fake_token", encoding="utf-8")

    cfg = _make_config(tmp_path, token_file=token_file)
    lifecycle = LifecycleManager(cfg)
    status = lifecycle.status()
    assert not status.mounted
    assert not lifecycle.is_mounted


def test_get_fuse_ops_before_start(config: Config) -> None:
    """get_fuse_ops returns None before pre_start."""
    lifecycle = LifecycleManager(config)
    assert lifecycle.get_fuse_ops() is None


def test_get_fuse_ops_after_start(config: Config) -> None:
    """get_fuse_ops returns ClawFUSE instance after pre_start."""
    config.ensure_dirs()
    lifecycle = LifecycleManager(config)

    with patch("clawfuse.lifecycle.DriveKitClient") as MockClient:
        mock_client = MagicMock()
        mock_client.list_all_files.return_value = []
        mock_client.list_files.return_value = {"files": []}
        MockClient.return_value = mock_client
        lifecycle.pre_start()

    from clawfuse.fuse import ClawFUSE

    ops = lifecycle.get_fuse_ops()
    assert ops is not None
    assert isinstance(ops, ClawFUSE)
