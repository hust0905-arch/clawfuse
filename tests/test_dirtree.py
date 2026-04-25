"""Tests for dirtree module."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from clawfuse.dirtree import DirTree, FileMeta


def test_refresh_builds_tree(mock_client: MagicMock, sample_files: list[dict]) -> None:
    """refresh loads and indexes all files."""
    mock_client.list_all_files.return_value = sample_files

    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    assert tree.file_count == 4
    assert tree.resolve("/data") is not None
    assert tree.resolve("/data/report.csv") is not None
    assert tree.resolve("/README.md") is not None
    assert tree.resolve("/docs") is not None


def test_resolve_root(mock_client: MagicMock) -> None:
    """resolve('/') returns None (root is not a file entry)."""
    mock_client.list_all_files.return_value = []
    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()
    assert tree.resolve("/") is None


def test_resolve_path(mock_client: MagicMock, sample_files: list[dict]) -> None:
    """resolve returns correct FileMeta for known paths."""
    mock_client.list_all_files.return_value = sample_files
    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    meta = tree.resolve("/data/report.csv")
    assert meta is not None
    assert meta.id == "file_report"
    assert meta.name == "report.csv"
    assert meta.is_dir is False
    assert meta.size == 1024
    assert meta.sha256 == "abc123"


def test_resolve_nonexistent(mock_client: MagicMock, sample_files: list[dict]) -> None:
    """resolve returns None for unknown paths."""
    mock_client.list_all_files.return_value = sample_files
    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    assert tree.resolve("/nonexistent.txt") is None
    assert tree.resolve("/data/nope.txt") is None


def test_list_dir_root(mock_client: MagicMock, sample_files: list[dict]) -> None:
    """list_dir('/') returns root children."""
    mock_client.list_all_files.return_value = sample_files
    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    children = tree.list_dir("/")
    assert "data" in children
    assert "docs" in children
    assert "README.md" in children


def test_list_dir_subfolder(mock_client: MagicMock, sample_files: list[dict]) -> None:
    """list_dir('/data') returns subfolder children."""
    mock_client.list_all_files.return_value = sample_files
    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    children = tree.list_dir("/data")
    assert children == ["report.csv"]


def test_get_path(mock_client: MagicMock, sample_files: list[dict]) -> None:
    """get_path returns path for a known file_id."""
    mock_client.list_all_files.return_value = sample_files
    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    assert tree.get_path("file_report") == "/data/report.csv"
    assert tree.get_path("folder_data") == "/data"
    assert tree.get_path("nonexistent") is None


def test_add_entry(mock_client: MagicMock) -> None:
    """add_entry inserts new file."""
    mock_client.list_all_files.return_value = []
    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    meta = FileMeta(
        id="new1", name="new.txt", is_dir=False, size=100, sha256="s1",
        parent_id="applicationData", modified_time="2026-01-01T00:00:00Z",
    )
    tree.add_entry("/new.txt", meta)

    assert tree.resolve("/new.txt") is not None
    assert tree.resolve("/new.txt").id == "new1"
    assert tree.file_count == 1


def test_remove_entry(mock_client: MagicMock, sample_files: list[dict]) -> None:
    """remove_entry removes a file."""
    mock_client.list_all_files.return_value = sample_files
    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    tree.remove_entry("/README.md")
    assert tree.resolve("/README.md") is None
    assert tree.get_path("file_readme") is None


def test_move_entry(mock_client: MagicMock, sample_files: list[dict]) -> None:
    """move_entry renames a file."""
    mock_client.list_all_files.return_value = sample_files
    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    tree.move_entry("/README.md", "/readme_v2.md")

    assert tree.resolve("/README.md") is None
    meta = tree.resolve("/readme_v2.md")
    assert meta is not None
    assert meta.name == "readme_v2.md"
    assert meta.id == "file_readme"


def test_hidden_files_filtered(mock_client: MagicMock) -> None:
    """Files starting with . are filtered out."""
    files = [
        {
            "id": "hidden1",
            "fileName": ".hidden",
            "mimeType": "text/plain",
            "parentFolder": [{"id": "applicationData"}],
        },
        {
            "id": "visible1",
            "fileName": "visible.txt",
            "mimeType": "text/plain",
            "parentFolder": [{"id": "applicationData"}],
        },
    ]
    mock_client.list_all_files.return_value = files

    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    assert tree.resolve("/.hidden") is None
    assert tree.resolve("/visible.txt") is not None
    assert tree.file_count == 1


def test_empty_drive(mock_client: MagicMock) -> None:
    """Empty Drive Kit returns empty tree."""
    mock_client.list_all_files.return_value = []

    tree = DirTree(mock_client, root_folder="applicationData", refresh_ttl=3600)
    tree.refresh()

    assert tree.file_count == 0
    assert tree.list_dir("/") == []
    assert tree.resolve("/anything") is None
