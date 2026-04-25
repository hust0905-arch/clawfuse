"""Tests for token module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawfuse.exceptions import TokenError
from clawfuse.token import TokenManager


# ── File mode (legacy) ──


def test_read_plain_text_token(token_file: Path) -> None:
    """Reads plain text access_token."""
    tm = TokenManager(token_file=token_file)
    assert tm.access_token == "fake_token_yaabhrt123456"


def test_read_json_token(token_file_json: Path) -> None:
    """Reads JSON format access_token."""
    tm = TokenManager(token_file=token_file_json)
    assert tm.access_token == "fake_json_token_xyz"


def test_missing_token_file(tmp_path: Path) -> None:
    """Raises TokenError for missing file."""
    tm = TokenManager(token_file=tmp_path / "nonexistent")
    with pytest.raises(TokenError, match="not found"):
        _ = tm.access_token


def test_empty_token_file(tmp_path: Path) -> None:
    """Raises TokenError for empty file."""
    path = tmp_path / "empty_token"
    path.write_text("")
    tm = TokenManager(token_file=path)
    with pytest.raises(TokenError, match="empty"):
        _ = tm.access_token


def test_caches_token(token_file: Path) -> None:
    """Token is cached in memory (not re-read every access)."""
    tm = TokenManager(token_file=token_file)
    t1 = tm.access_token
    # Overwrite file
    token_file.write_text("new_token_999")
    # Should still return cached value (within _REREAD_INTERVAL)
    t2 = tm.access_token
    assert t1 == t2


def test_force_reread(token_file: Path) -> None:
    """force_reread re-reads the token file."""
    tm = TokenManager(token_file=token_file)
    t1 = tm.access_token
    # Overwrite file
    token_file.write_text("brand_new_token_xyz")
    # Force re-read
    t2 = tm.force_reread()
    assert t2 == "brand_new_token_xyz"
    assert t1 != t2


def test_token_file_path_property(token_file: Path) -> None:
    """token_file_path returns the configured path."""
    tm = TokenManager(token_file=token_file)
    assert tm.token_file_path == token_file


def test_json_without_access_token(tmp_path: Path) -> None:
    """Raises TokenError for JSON without access_token field."""
    path = tmp_path / "bad_token"
    path.write_text(json.dumps({"refresh_token": "abc"}))
    tm = TokenManager(token_file=path)
    with pytest.raises(TokenError, match="no access_token"):
        _ = tm.access_token


# ── String mode (from JSON config) ──


def test_from_string_basic() -> None:
    """from_string creates TokenManager with direct token."""
    tm = TokenManager.from_string("my_access_token_123")
    assert tm.access_token == "my_access_token_123"


def test_from_string_always_same() -> None:
    """String token always returns the same value."""
    tm = TokenManager.from_string("stable_token")
    assert tm.access_token == "stable_token"
    assert tm.access_token == "stable_token"


def test_from_string_force_reread_noop() -> None:
    """force_reread on string mode returns same token."""
    tm = TokenManager.from_string("immutable_token")
    result = tm.force_reread()
    assert result == "immutable_token"


def test_from_string_no_file_path() -> None:
    """String mode has no token_file_path."""
    tm = TokenManager.from_string("abc")
    assert tm.token_file_path is None


def test_direct_constructor_string() -> None:
    """TokenManager(token_string=...) works."""
    tm = TokenManager(token_string="direct_token")
    assert tm.access_token == "direct_token"


def test_direct_constructor_file(token_file: Path) -> None:
    """TokenManager(token_file=...) works."""
    tm = TokenManager(token_file=token_file)
    assert tm.access_token == "fake_token_yaabhrt123456"


def test_no_args_raises() -> None:
    """TokenManager with no args raises TokenError."""
    with pytest.raises(TokenError):
        TokenManager()


def test_empty_string_raises() -> None:
    """TokenManager with empty string raises at construction."""
    with pytest.raises(TokenError):
        TokenManager(token_string="")
