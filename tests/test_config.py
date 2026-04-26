"""Tests for config module."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from clawfuse.config import Config
from clawfuse.exceptions import ConfigError


# ── from_env (legacy) ──


def test_from_env_with_required_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Config loads from environment variables."""
    token_file = tmp_path / "token"
    token_file.write_text("tok123")
    monkeypatch.setenv("CLAWFUSE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CLAWFUSE_CACHE_DIR", str(tmp_path / "cache"))

    config = Config.from_env()
    assert config.token_file == token_file
    assert config.cache_dir == tmp_path / "cache"
    assert config.token_string == ""


def test_from_env_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Config has correct defaults."""
    monkeypatch.setenv("CLAWFUSE_TOKEN_FILE", str(tmp_path / "token"))

    config = Config.from_env()
    assert config.mount_point == "/mnt/drive"
    assert config.root_folder == "applicationData"
    assert config.cloud_folder == "applicationData"
    assert config.cache_max_bytes == 512 * 1024 * 1024
    assert config.cache_max_files == 500
    assert config.drain_interval == 5.0
    assert config.http_timeout == 30


def test_from_env_cache_max_mb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cache_max_bytes is computed from MB value."""
    monkeypatch.setenv("CLAWFUSE_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("CLAWFUSE_CACHE_MAX_MB", "100")

    config = Config.from_env()
    assert config.cache_max_bytes == 100 * 1024 * 1024


# ── from_file (JSON config) ──


def test_from_file_basic(config_json_file: Path) -> None:
    """Config loads from JSON file with token."""
    config = Config.from_file(config_json_file)

    assert config.token_string == "test_token_from_json_xyz"
    assert config.token_file is None
    assert config.cloud_folder == "workspace"
    assert config.cache_max_bytes == 10 * 1024 * 1024
    assert config.cache_max_files == 50
    assert config.drain_interval == 2.0


def test_from_file_missing_path(tmp_path: Path) -> None:
    """from_file raises ConfigError for missing file."""
    with pytest.raises(ConfigError, match="not found"):
        Config.from_file(tmp_path / "nonexistent.json")


def test_from_file_invalid_json(tmp_path: Path) -> None:
    """from_file raises ConfigError for invalid JSON."""
    path = tmp_path / "bad.json"
    path.write_text("{invalid json", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid JSON"):
        Config.from_file(path)


def test_from_file_missing_token(tmp_path: Path) -> None:
    """from_file raises ConfigError when token is missing."""
    path = tmp_path / "no_token.json"
    path.write_text(json.dumps({"mount_point": "/mnt"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="token"):
        Config.from_file(path)


def test_from_file_empty_token(tmp_path: Path) -> None:
    """from_file raises ConfigError when token is empty."""
    path = tmp_path / "empty_token.json"
    path.write_text(json.dumps({"token": "  "}), encoding="utf-8")
    with pytest.raises(ConfigError, match="token"):
        Config.from_file(path)


def test_from_file_defaults(tmp_path: Path) -> None:
    """from_file uses defaults for omitted fields."""
    path = tmp_path / "minimal.json"
    path.write_text(json.dumps({"token": "abc123"}), encoding="utf-8")

    config = Config.from_file(path)
    assert config.token_string == "abc123"
    assert config.cloud_folder == "applicationData"
    assert config.root_folder == "applicationData"
    assert config.mount_point == "/mnt/drive"
    assert config.log_level == "INFO"


def test_from_file_cloud_folder_id(tmp_path: Path) -> None:
    """from_file recognizes long cloud_folder as folder ID."""
    path = tmp_path / "id.json"
    folder_id = "Bm2to6L_z8ULgmURYI5NB_p64kBdI8_NC"
    path.write_text(json.dumps({"token": "abc", "cloud_folder": folder_id}), encoding="utf-8")

    config = Config.from_file(path)
    assert config.cloud_folder == folder_id
    assert config.root_folder == folder_id  # Resolved immediately


def test_from_file_cloud_folder_name(tmp_path: Path) -> None:
    """from_file sets root_folder to applicationData when cloud_folder is a name."""
    path = tmp_path / "name.json"
    path.write_text(json.dumps({"token": "abc", "cloud_folder": "workspace"}), encoding="utf-8")

    config = Config.from_file(path)
    assert config.cloud_folder == "workspace"
    assert config.root_folder == "applicationData"  # Will be resolved at runtime
    assert config.needs_folder_resolution is True


def test_from_file_no_folder_resolution_needed(tmp_path: Path) -> None:
    """applicationData does not need folder resolution."""
    path = tmp_path / "default.json"
    path.write_text(json.dumps({"token": "abc"}), encoding="utf-8")

    config = Config.from_file(path)
    assert config.needs_folder_resolution is False


# ── frozen dataclass ──


def test_frozen_dataclass(tmp_path: Path) -> None:
    """Config is immutable."""
    config = Config(
        token_file=tmp_path / "token",
        token_string="",
        cloud_folder="applicationData",
        mount_point="/mnt",
        root_folder="app",
        cache_dir=tmp_path / "cache",
        cache_max_bytes=1024,
        cache_max_files=10,
        write_buf_dir=tmp_path / "buf",
        drain_interval=5.0,
        drain_max_retries=3,
        tree_refresh_ttl=10.0,
        list_page_size=100,
        http_timeout=30,
        log_level="INFO",
    )
    with pytest.raises(AttributeError):
        config.mount_point = "/other"  # type: ignore[misc]


# ── validate ──


def test_validate_success(tmp_path: Path) -> None:
    """validate passes with valid config (file mode)."""
    config = Config(
        token_file=tmp_path / "token",
        mount_point="/mnt",
        root_folder="app",
        cache_dir=tmp_path / "cache",
        cache_max_bytes=1024,
        cache_max_files=10,
        write_buf_dir=tmp_path / "buf",
        drain_interval=5.0,
        drain_max_retries=3,
        tree_refresh_ttl=10.0,
        list_page_size=100,
        http_timeout=30,
        log_level="INFO",
    )
    config.validate()  # Should not raise


def test_validate_string_token(tmp_path: Path) -> None:
    """validate passes with string token (no file)."""
    config = Config(
        token_string="abc123",
        mount_point="/mnt",
        root_folder="app",
        cache_dir=tmp_path / "cache",
        cache_max_bytes=1024,
        cache_max_files=10,
        write_buf_dir=tmp_path / "buf",
        drain_interval=5.0,
        drain_max_retries=3,
        tree_refresh_ttl=10.0,
        list_page_size=100,
        http_timeout=30,
        log_level="INFO",
    )
    config.validate()  # Should not raise


def test_validate_no_token(tmp_path: Path) -> None:
    """validate rejects config with no token."""
    config = Config(
        token_file=None,
        token_string="",
        mount_point="/mnt",
        root_folder="app",
        cache_dir=tmp_path / "cache",
        cache_max_bytes=1024,
        cache_max_files=10,
        write_buf_dir=tmp_path / "buf",
        drain_interval=5.0,
        drain_max_retries=3,
        tree_refresh_ttl=10.0,
        list_page_size=100,
        http_timeout=30,
        log_level="INFO",
    )
    with pytest.raises(ConfigError, match="token"):
        config.validate()


def test_validate_zero_cache(tmp_path: Path) -> None:
    """validate rejects zero cache_max_bytes."""
    config = Config(
        token_file=tmp_path / "token",
        mount_point="/mnt",
        root_folder="app",
        cache_dir=tmp_path / "cache",
        cache_max_bytes=0,
        cache_max_files=10,
        write_buf_dir=tmp_path / "buf",
        drain_interval=5.0,
        drain_max_retries=3,
        tree_refresh_ttl=10.0,
        list_page_size=100,
        http_timeout=30,
        log_level="INFO",
    )
    with pytest.raises(ConfigError, match="cache_max_bytes"):
        config.validate()


# ── ensure_dirs ──


def test_ensure_dirs(tmp_path: Path) -> None:
    """ensure_dirs creates directories."""
    config = Config(
        token_file=tmp_path / "token",
        mount_point=str(tmp_path / "mnt"),
        root_folder="app",
        cache_dir=tmp_path / "cache",
        cache_max_bytes=1024,
        cache_max_files=10,
        write_buf_dir=tmp_path / "buf",
        drain_interval=5.0,
        drain_max_retries=3,
        tree_refresh_ttl=10.0,
        list_page_size=100,
        http_timeout=30,
        log_level="INFO",
    )
    config.ensure_dirs()
    assert (tmp_path / "cache").is_dir()
    assert (tmp_path / "buf").is_dir()
