"""Drive Kit REST API client — simplified for ClawFUSE.

Only includes operations needed for single-container FUSE mount:
create, update, get, download, delete, list, create_folder.

No locks, events, batch, search, subscribe, or history versions.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from .config import API_BASE, FOLDER_MIME, UPLOAD_BASE
from .exceptions import DriveKitError, TokenError
from .token import TokenManager

logger = logging.getLogger(__name__)

DEFAULT_FIELDS = "id,fileName,mimeType,sha256,size,parentFolder,modifiedTime"


class DriveKitClient:
    """Simplified Drive Kit REST API client for ClawFUSE."""

    def __init__(self, token_manager: TokenManager, timeout: int = 30) -> None:
        self._token = token_manager
        self._timeout = timeout

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.access_token}"}

    @staticmethod
    def _params(**extra: Any) -> dict[str, Any]:
        p: dict[str, Any] = {"containers": "applicationData"}
        p.update(extra)
        return p

    def _check(self, resp: requests.Response) -> dict:
        """Check response status, return parsed JSON."""
        if resp.ok:
            return resp.json()
        raise DriveKitError(resp.status_code, resp.text[:500])

    def _check_status(self, resp: requests.Response) -> None:
        """Check response status, raise on error (no JSON body expected)."""
        if not resp.ok:
            raise DriveKitError(resp.status_code, resp.text[:500])

    def _retry_on_401(self, fn: Any) -> Any:
        """Call fn(), retry once on 401 after re-reading token file.

        Circuit breaker: once token is confirmed expired (401 on retry too),
        marks it as dead so all subsequent calls fail immediately.
        """
        # Fast fail if token is already known dead
        if self._token.is_dead:
            raise TokenError("Token expired — cannot be refreshed. Restart with a new token.")

        try:
            return fn()
        except DriveKitError as e:
            if e.status_code == 401:
                logger.info("Token expired (401), attempting refresh")
                self._token.force_reread()
                try:
                    result = fn()
                    # Retry succeeded — token is still valid, unmark dead if needed
                    return result
                except DriveKitError as e2:
                    if e2.status_code == 401:
                        # Confirmed: token is expired and cannot be refreshed
                        self._token.mark_dead()
                        raise TokenError(
                            "Token expired and cannot be refreshed — "
                            "update the token file or restart with a new token"
                        ) from e2
                    raise
            raise

    # ── File CRUD ──

    def create_file(
        self,
        filename: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        parent_folder: str = "applicationData",
        fields: str = DEFAULT_FIELDS,
    ) -> dict:
        """Create file with content via multipart/related upload."""
        boundary = f"clawfuse_{int(time.time() * 1000)}"
        meta = {
            "fileName": filename,
            "mimeType": mime_type,
            "parentFolder": [parent_folder],
        }
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--".encode()

        def _do() -> dict:
            resp = requests.post(
                f"{UPLOAD_BASE}/files",
                headers={**self._auth, "Content-Type": f"multipart/related; boundary={boundary}"},
                params=self._params(uploadType="multipart", fields=fields),
                data=body,
                timeout=self._timeout,
            )
            return self._check(resp)

        return self._retry_on_401(_do)

    def update_file(
        self,
        file_id: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        fields: str = DEFAULT_FIELDS,
    ) -> dict:
        """Update file content via multipart/related PATCH."""
        boundary = f"clawfuse_u_{int(time.time() * 1000)}"
        meta: dict[str, Any] = {}

        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--".encode()

        def _do() -> dict:
            resp = requests.patch(
                f"{UPLOAD_BASE}/files/{file_id}",
                headers={**self._auth, "Content-Type": f"multipart/related; boundary={boundary}"},
                params=self._params(uploadType="multipart", fields=fields),
                data=body,
                timeout=self._timeout,
            )
            return self._check(resp)

        return self._retry_on_401(_do)

    def get_file(self, file_id: str, fields: str = DEFAULT_FIELDS) -> dict:
        """Get file metadata."""

        def _do() -> dict:
            resp = requests.get(
                f"{API_BASE}/files/{file_id}",
                headers=self._auth,
                params=self._params(fields=fields),
                timeout=self._timeout,
            )
            return self._check(resp)

        return self._retry_on_401(_do)

    def download_file(self, file_id: str) -> bytes:
        """Download file content."""

        def _do() -> bytes:
            resp = requests.get(
                f"{API_BASE}/files/{file_id}",
                headers=self._auth,
                params=self._params(form="content"),
                timeout=self._timeout,
            )
            self._check_status(resp)
            return resp.content

        return self._retry_on_401(_do)

    def delete_file(self, file_id: str) -> None:
        """Delete a file (move to trash)."""

        def _do() -> None:
            resp = requests.delete(
                f"{API_BASE}/files/{file_id}",
                headers=self._auth,
                params=self._params(),
                timeout=self._timeout,
            )
            if resp.status_code not in (200, 204):
                raise DriveKitError(resp.status_code, resp.text[:500])

        self._retry_on_401(_do)

    # ── Folder ──

    def create_folder(
        self,
        folder_name: str,
        parent_folder: str = "applicationData",
        fields: str = "id,fileName",
    ) -> dict:
        """Create a folder."""
        meta = {
            "fileName": folder_name,
            "mimeType": FOLDER_MIME,
            "parentFolder": [parent_folder],
        }

        def _do() -> dict:
            resp = requests.post(
                f"{API_BASE}/files",
                headers={**self._auth, "Content-Type": "application/json"},
                params=self._params(fields=fields),
                json=meta,
                timeout=self._timeout,
            )
            return self._check(resp)

        return self._retry_on_401(_do)

    # ── List ──

    def list_files(
        self,
        parent_folder: str | None = None,
        page_size: int = 100,
        fields: str = f"files({DEFAULT_FIELDS}),nextCursor",
        cursor: str | None = None,
    ) -> dict:
        """List files with pagination. Returns {files: [...], nextCursor: '...'}.

        Uses Drive Kit queryParam filter: '{folderId}' in parentFolder.
        Without parent_folder, lists all files in the container.
        """
        p = self._params(pageSize=str(page_size), fields=fields)
        if parent_folder:
            p["queryParam"] = f"'{parent_folder}' in parentFolder"
        if cursor:
            p["pageCursor"] = cursor

        def _do() -> dict:
            resp = requests.get(
                f"{API_BASE}/files",
                headers=self._auth,
                params=p,
                timeout=self._timeout,
            )
            return self._check(resp)

        return self._retry_on_401(_do)

    def list_all_files(
        self,
        root_folder: str = "applicationData",
        page_size: int = 100,
    ) -> list[dict]:
        """Recursively list all files starting from root_folder.

        Uses BFS to traverse folder hierarchy. Returns flat list of all files/folders.
        """
        all_items: list[dict] = []
        folders_to_process: list[str] = [root_folder]
        seen_ids: set[str] = set()

        while folders_to_process:
            folder_id = folders_to_process.pop(0)
            cursor: str | None = None
            prev_cursor: str | None = None

            while True:
                result = self.list_files(
                    parent_folder=folder_id,
                    page_size=page_size,
                    cursor=cursor,
                )
                files = result.get("files", [])

                # Deduplicate items and stop if no new items
                new_files = []
                new_ids: set[str] = set()
                for f in files:
                    fid = f.get("id", "")
                    if fid and fid not in seen_ids:
                        seen_ids.add(fid)
                        new_files.append(f)
                        new_ids.add(fid)

                all_items.extend(new_files)

                # Collect sub-folders for BFS
                for f in new_files:
                    if f.get("mimeType") == FOLDER_MIME:
                        folders_to_process.append(f["id"])

                cursor = result.get("nextCursor")
                if not cursor or cursor == prev_cursor or not new_ids:
                    break
                prev_cursor = cursor

        logger.info("Loaded %d items from Drive Kit (root=%s)", len(all_items), root_folder)
        return all_items

    # ── Metadata ──

    def update_metadata(self, file_id: str, **meta: Any) -> dict:
        """Update file metadata (fileName, parentFolder, etc.)."""

        def _do() -> dict:
            resp = requests.patch(
                f"{API_BASE}/files/{file_id}",
                headers={**self._auth, "Content-Type": "application/json"},
                params=self._params(fields="id,fileName"),
                json=meta,
                timeout=self._timeout,
            )
            return self._check(resp)

        return self._retry_on_401(_do)
