"""Tests for client module — mock HTTP requests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from clawfuse.client import DriveKitClient
from clawfuse.exceptions import DriveKitError, TokenError
from clawfuse.token import TokenManager


@pytest.fixture
def token_mgr(token_file: Path) -> TokenManager:
    return TokenManager(token_file)


@pytest.fixture
def client(token_mgr: TokenManager) -> DriveKitClient:
    return DriveKitClient(token_mgr, timeout=10)


def test_create_file(client: DriveKitClient) -> None:
    """create_file sends multipart POST."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"id": "new1", "fileName": "test.txt", "sha256": "abc"}
    mock_resp.status_code = 200

    with patch("clawfuse.client.requests.post", return_value=mock_resp) as mock_post:
        result = client.create_file("test.txt", b"hello", parent_folder="root123")
        assert result["id"] == "new1"
        mock_post.assert_called_once()
        # Verify multipart content type
        call_kwargs = mock_post.call_args
        assert "multipart/related" in call_kwargs.kwargs.get("headers", {}).get("Content-Type", "")


def test_update_file(client: DriveKitClient) -> None:
    """update_file sends multipart PATCH."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"id": "file1", "sha256": "new_sha"}
    mock_resp.status_code = 200

    with patch("clawfuse.client.requests.patch", return_value=mock_resp) as mock_patch:
        result = client.update_file("file1", b"updated content")
        assert result["id"] == "file1"
        mock_patch.assert_called_once()


def test_get_file(client: DriveKitClient) -> None:
    """get_file sends GET and returns metadata."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"id": "file1", "fileName": "test.txt"}
    mock_resp.status_code = 200

    with patch("clawfuse.client.requests.get", return_value=mock_resp) as mock_get:
        result = client.get_file("file1")
        assert result["fileName"] == "test.txt"
        mock_get.assert_called_once()


def test_download_file(client: DriveKitClient) -> None:
    """download_file returns file content bytes."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.content = b"file content here"
    mock_resp.status_code = 200

    with patch("clawfuse.client.requests.get", return_value=mock_resp) as mock_get:
        content = client.download_file("file1")
        assert content == b"file content here"


def test_delete_file(client: DriveKitClient) -> None:
    """delete_file sends DELETE."""
    mock_resp = MagicMock()
    mock_resp.status_code = 204

    with patch("clawfuse.client.requests.delete", return_value=mock_resp) as mock_del:
        client.delete_file("file1")
        mock_del.assert_called_once()


def test_create_folder(client: DriveKitClient) -> None:
    """create_folder sends POST with folder MIME."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"id": "folder1", "fileName": "new_dir"}

    with patch("clawfuse.client.requests.post", return_value=mock_resp) as mock_post:
        result = client.create_folder("new_dir", parent_folder="root")
        assert result["id"] == "folder1"
        # Verify the body contains folder MIME type
        call_kwargs = mock_post.call_args
        body = call_kwargs.kwargs.get("json", {})
        assert body["mimeType"] == "application/vnd.huawei-apps.folder"


def test_list_files(client: DriveKitClient) -> None:
    """list_files sends GET with pagination params."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"files": [{"id": "f1"}], "nextCursor": "cursor1"}

    with patch("clawfuse.client.requests.get", return_value=mock_resp) as mock_get:
        result = client.list_files(parent_folder="root", page_size=100)
        assert len(result["files"]) == 1
        assert result["nextCursor"] == "cursor1"


def test_api_error_raises(client: DriveKitClient) -> None:
    """Non-OK response raises DriveKitError."""
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 404
    mock_resp.text = "Not found"

    with patch("clawfuse.client.requests.get", return_value=mock_resp):
        with pytest.raises(DriveKitError) as exc_info:
            client.get_file("nonexistent")
        assert exc_info.value.status_code == 404


def test_401_retry(client: DriveKitClient, token_file: Path) -> None:
    """401 triggers token re-read and retry."""
    # First response: 401
    resp_401 = MagicMock()
    resp_401.ok = False
    resp_401.status_code = 401
    resp_401.text = "Unauthorized"

    # Second response: success
    resp_ok = MagicMock()
    resp_ok.ok = True
    resp_ok.json.return_value = {"id": "file1", "fileName": "test.txt"}

    with patch("clawfuse.client.requests.get", side_effect=[resp_401, resp_ok]) as mock_get:
        result = client.get_file("file1")
        assert result["fileName"] == "test.txt"
        assert mock_get.call_count == 2  # Retried once


def test_list_all_files(client: DriveKitClient) -> None:
    """list_all_files recursively fetches all pages."""
    page1 = {
        "files": [
            {"id": "f1", "fileName": "a.txt", "mimeType": "text/plain"},
            {"id": "d1", "fileName": "subdir", "mimeType": "application/vnd.huawei-apps.folder",
             "parentFolder": [{"id": "root"}]},
        ],
    }
    page2 = {
        "files": [
            {"id": "f2", "fileName": "b.txt", "mimeType": "text/plain"},
        ],
    }

    with patch.object(client, "list_files", side_effect=[page1, page2]) as mock_list:
        result = client.list_all_files(root_folder="root")
        assert len(result) == 3
        assert mock_list.call_count == 2


# ── Circuit breaker tests ──


def test_401_circuit_breaker_marks_dead(client: DriveKitClient, token_file: Path) -> None:
    """Persistent 401 marks token as dead — subsequent calls fail immediately."""
    resp_401 = MagicMock()
    resp_401.ok = False
    resp_401.status_code = 401
    resp_401.text = "Unauthorized"

    with patch("clawfuse.client.requests.get", return_value=resp_401):
        # First call: 401 → retry → 401 → mark dead → TokenError
        with pytest.raises(TokenError, match="Token expired"):
            client.get_file("file1")

    assert client._token.is_dead

    # Second call: immediate TokenError without HTTP request
    with pytest.raises(TokenError, match="Token expired"):
        client.get_file("file2")


def test_string_mode_cannot_refresh() -> None:
    """String mode token cannot be refreshed via force_reread."""
    mgr = TokenManager.from_string("my_static_token")
    assert mgr.access_token == "my_static_token"
    assert mgr.current_token == "my_static_token"

    # force_reread returns same token
    result = mgr.force_reread()
    assert result == "my_static_token"
    assert not mgr.is_dead


def test_token_mark_dead() -> None:
    """mark_dead() causes access_token to raise TokenError."""
    mgr = TokenManager.from_string("my_token")
    assert mgr.access_token == "my_token"

    mgr.mark_dead()
    assert mgr.is_dead

    with pytest.raises(TokenError, match="Token expired"):
        _ = mgr.access_token


def test_current_token_property() -> None:
    """current_token returns value without dead check."""
    mgr = TokenManager.from_string("my_token")
    assert mgr.current_token == "my_token"

    mgr.mark_dead()
    # current_token still works (no dead check)
    assert mgr.current_token == "my_token"
