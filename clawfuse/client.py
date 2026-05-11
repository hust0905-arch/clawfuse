"""Drive Kit REST API client — simplified for ClawFUSE.

Only includes operations needed for single-container FUSE mount:
create, update, get, download, delete, list, create_folder.

No locks, events, batch, search, subscribe, or history versions.
"""

from __future__ import annotations

import json
import logging
import threading
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

    def __init__(self, token_manager: TokenManager, timeout: int = 30, max_concurrent: int = 8) -> None:
        self._token = token_manager
        self._timeout = timeout
        # Limit concurrent API calls to prevent thread explosion when
        # background BFS and FUSE operations both hit the API simultaneously.
        self._semaphore = threading.Semaphore(max_concurrent)

    def _upload_timeout(self, content_size: int) -> int:
        """Calculate upload timeout based on content size.

        Base timeout + 3 seconds per MB, capped at 10 minutes.
        """
        mb = content_size / (1024 * 1024)
        return min(self._timeout + int(mb * 3.0), 600)

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

        Rate-limits concurrent API calls via semaphore to prevent thread
        explosion when background BFS and FUSE operations hit the API
        simultaneously. Without this limit, slow API responses cause all
        threads to block, making the filesystem appear hung.

        Circuit breaker: once token is confirmed expired (401 on retry too),
        marks it as dead so all subsequent calls fail immediately.
        Revival: if an external process updates the token file/config,
        try_revive() detects the change and resets the circuit breaker.
        """
        # Fast fail if token is dead — but check for external token updates first
        if self._token.is_dead:
            if self._token.try_revive():
                logger.info("Token revived from external update, retrying API call")
            else:
                raise TokenError("Token expired — cannot be refreshed. Restart with a new token.")

        with self._semaphore:
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
                timeout=self._upload_timeout(len(content)),
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
                timeout=self._upload_timeout(len(content)),
            )
            return self._check(resp)

        return self._retry_on_401(_do)

    # ── Resumable (chunked) upload ──

    def _initiate_resumable(
        self,
        method: str,
        url: str,
        meta: dict,
        total_size: int,
        content_type: str = "application/octet-stream",
        fields: str = DEFAULT_FIELDS,
    ) -> str:
        """Initiate a resumable upload session. Returns upload URI."""

        def _do() -> str:
            resp = requests.request(
                method,
                url,
                headers={
                    **self._auth,
                    "Content-Type": "application/json",
                    "X-Upload-Content-Type": content_type,
                    "X-Upload-Content-Length": str(total_size),
                },
                params=self._params(uploadType="resume", fields=fields),
                json=meta,
                timeout=self._timeout,
            )
            self._check_status(resp)
            location = resp.headers.get("Location") or resp.headers.get("location")
            if not location:
                raise DriveKitError(
                    resp.status_code,
                    "No Location header in resumable upload initiation response",
                )
            return location

        return self._retry_on_401(_do)

    def _upload_chunks(
        self,
        upload_uri: str,
        content: bytes,
        chunk_size: int,
        max_chunk_retries: int = 3,
    ) -> dict:
        """Upload content in chunks to a resumable upload URI.

        Each chunk is a PUT with Content-Range header.  Intermediate chunks
        return 308 Resume Incomplete; the final chunk returns 200 with file
        metadata.
        """
        total_size = len(content)
        offset = 0

        while offset < total_size:
            end = min(offset + chunk_size, total_size)
            chunk = content[offset:end]

            for attempt in range(max_chunk_retries):
                try:
                    resp = requests.put(
                        upload_uri,
                        headers={
                            **self._auth,
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{end - 1}/{total_size}",
                        },
                        data=chunk,
                        timeout=self._upload_timeout(chunk_size),
                    )
                    break
                except (requests.ConnectionError, requests.Timeout) as exc:
                    if attempt == max_chunk_retries - 1:
                        raise
                    wait = 1.0 * (attempt + 1)
                    logger.warning(
                        "Chunk upload failed (attempt %d/%d, offset %d): %s — retrying in %.1fs",
                        attempt + 1, max_chunk_retries, offset, exc, wait,
                    )
                    time.sleep(wait)

            if resp.status_code == 308:
                # Partial success — server tells us how far it got
                range_hdr = resp.headers.get("Range", resp.headers.get("range", ""))
                if range_hdr and "-" in range_hdr:
                    offset = int(range_hdr.split("-")[-1]) + 1
                else:
                    offset = end
                continue

            if resp.ok:
                return resp.json()

            raise DriveKitError(resp.status_code, resp.text[:500])

        raise DriveKitError(500, "Resumable upload ended without final response")

    def create_file_resumable(
        self,
        filename: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        parent_folder: str = "applicationData",
        fields: str = DEFAULT_FIELDS,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> dict:
        """Create file using resumable (chunked) upload for large files."""
        meta = {
            "fileName": filename,
            "mimeType": mime_type,
            "parentFolder": [parent_folder],
        }
        upload_uri = self._initiate_resumable(
            "POST",
            f"{UPLOAD_BASE}/files",
            meta,
            total_size=len(content),
            content_type=mime_type,
            fields=fields,
        )
        return self._upload_chunks(upload_uri, content, chunk_size)

    def update_file_resumable(
        self,
        file_id: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        fields: str = DEFAULT_FIELDS,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> dict:
        """Update file content using resumable (chunked) upload for large files."""
        upload_uri = self._initiate_resumable(
            "PATCH",
            f"{UPLOAD_BASE}/files/{file_id}",
            {},
            total_size=len(content),
            content_type=mime_type,
            fields=fields,
        )
        return self._upload_chunks(upload_uri, content, chunk_size)

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
