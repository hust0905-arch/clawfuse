"""Shared pytest fixtures for ClawFUSE tests."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from clawfuse.config import Config


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory."""
    return tmp_path


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    """Create a temporary token file with a fake access_token."""
    path = tmp_path / "access_token"
    path.write_text("fake_token_yaabhrt123456", encoding="utf-8")
    return path


@pytest.fixture
def token_file_json(tmp_path: Path) -> Path:
    """Create a temporary token file in JSON format."""
    path = tmp_path / "token.json"
    data = {"access_token": "fake_json_token_xyz", "expires_at": 9999999999}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def config(tmp_path: Path, token_file: Path) -> Config:
    """Create a Config with temporary directories (file-based token)."""
    return Config(
        token_file=token_file,
        token_string="",
        cloud_folder="applicationData",
        mount_point=str(tmp_path / "mnt"),
        root_folder="applicationData",
        cache_dir=tmp_path / "cache",
        cache_max_bytes=10 * 1024 * 1024,  # 10MB
        cache_max_files=100,
        write_buf_dir=tmp_path / "writes",
        drain_interval=1.0,
        drain_max_retries=3,
        tree_refresh_ttl=3600,  # Long TTL for tests
        list_page_size=200,
        http_timeout=10,
        log_level="DEBUG",
    )


@pytest.fixture
def config_with_string_token(tmp_path: Path) -> Config:
    """Create a Config with a direct token string (JSON config mode)."""
    return Config(
        token_file=None,
        token_string="fake_string_token_abc123",
        cloud_folder="applicationData",
        mount_point=str(tmp_path / "mnt"),
        root_folder="applicationData",
        cache_dir=tmp_path / "cache",
        cache_max_bytes=10 * 1024 * 1024,
        cache_max_files=100,
        write_buf_dir=tmp_path / "writes",
        drain_interval=1.0,
        drain_max_retries=3,
        tree_refresh_ttl=3600,
        list_page_size=200,
        http_timeout=10,
        log_level="DEBUG",
    )


@pytest.fixture
def config_json_file(tmp_path: Path) -> Path:
    """Create a temporary JSON config file."""
    config_data = {
        "token": "test_token_from_json_xyz",
        "cloud_folder": "workspace",
        "mount_point": str(tmp_path / "mnt"),
        "cache_dir": str(tmp_path / "cache"),
        "cache_max_mb": 10,
        "cache_max_files": 50,
        "write_buf_dir": str(tmp_path / "writes"),
        "drain_interval": 2.0,
        "log_level": "DEBUG",
    }
    path = tmp_path / "clawfuse.json"
    path.write_text(json.dumps(config_data, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def mock_client() -> MagicMock:
    """Create a mock DriveKitClient."""
    client = MagicMock()
    client.download_file.return_value = b"hello world"
    client.create_file.return_value = {
        "id": "new_file_001",
        "fileName": "test.txt",
        "sha256": "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
        "size": 12,
    }
    client.update_file.return_value = {
        "id": "file_001",
        "sha256": "updated_sha256",
        "size": 20,
    }
    client.create_folder.return_value = {
        "id": "new_folder_001",
        "fileName": "test_dir",
    }
    client.list_all_files.return_value = []
    return client


@pytest.fixture
def sample_files() -> list[dict]:
    """Sample Drive Kit file listing for tests."""
    return [
        {
            "id": "folder_data",
            "fileName": "data",
            "mimeType": "application/vnd.huawei-apps.folder",
            "sha256": "",
            "size": 0,
            "parentFolder": [{"id": "applicationData"}],
            "modifiedTime": "2026-04-24T10:00:00Z",
        },
        {
            "id": "file_report",
            "fileName": "report.csv",
            "mimeType": "text/csv",
            "sha256": "abc123",
            "size": 1024,
            "parentFolder": [{"id": "folder_data"}],
            "modifiedTime": "2026-04-24T10:30:00Z",
        },
        {
            "id": "file_readme",
            "fileName": "README.md",
            "mimeType": "text/markdown",
            "sha256": "def456",
            "size": 512,
            "parentFolder": [{"id": "applicationData"}],
            "modifiedTime": "2026-04-24T09:00:00Z",
        },
        {
            "id": "folder_docs",
            "fileName": "docs",
            "mimeType": "application/vnd.huawei-apps.folder",
            "sha256": "",
            "size": 0,
            "parentFolder": [{"id": "applicationData"}],
            "modifiedTime": "2026-04-24T08:00:00Z",
        },
    ]
