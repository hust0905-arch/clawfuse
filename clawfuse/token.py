"""Token management — read-only access_token from file or direct string."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .exceptions import TokenError

logger = logging.getLogger(__name__)

# Re-read the token file if it was last read more than this many seconds ago
_REREAD_INTERVAL = 60.0


class TokenManager:
    """Manages access_token for Drive Kit API.

    Supports two modes:
    1. Direct token string (from JSON config) — always returns the same value
    2. Token file (from env/legacy) — reads from file, re-reads periodically

    Circuit breaker: once the token is confirmed expired (401) and cannot be
    refreshed, mark_dead() sets a flag that causes all subsequent API calls to
    fail immediately instead of waiting for HTTP timeouts.
    """

    def __init__(
        self,
        token_file: Path | None = None,
        token_string: str = "",
    ) -> None:
        if token_string:
            self._mode = "string"
            self._token = token_string
            self._token_file: Path | None = None
        elif token_file is not None:
            self._mode = "file"
            self._token = ""
            self._token_file = token_file
        else:
            raise TokenError("Either token_file or token_string must be provided")

        self._last_read_time: float = 0.0
        self._dead: bool = False

    @classmethod
    def from_string(cls, token: str) -> TokenManager:
        """Create a TokenManager with a direct token string."""
        return cls(token_string=token)

    @classmethod
    def from_file(cls, path: Path) -> TokenManager:
        """Create a TokenManager that reads from a file."""
        return cls(token_file=path)

    @property
    def access_token(self) -> str:
        """Get a valid access_token. Raises TokenError if token is dead."""
        if self._dead:
            raise TokenError("Token expired and cannot be refreshed — restart with a new token")

        if self._mode == "string":
            if not self._token:
                raise TokenError("Token string is empty")
            return self._token

        # File mode — re-read if stale
        if self._is_stale:
            self._read_token_file()
        if not self._token:
            raise TokenError(f"Token file is empty or missing: {self._token_file}")
        return self._token

    @property
    def is_dead(self) -> bool:
        """Whether the token has been confirmed expired and cannot be refreshed."""
        return self._dead

    @property
    def current_token(self) -> str:
        """Current token value without triggering refresh or dead check.

        Used for comparison (e.g., detecting if force_reread() got a new value).
        """
        return self._token

    def mark_dead(self) -> None:
        """Mark token as expired/unrecoverable. All subsequent API calls fail fast."""
        self._dead = True
        logger.error("Token marked as dead — all subsequent API calls will fail immediately")

    def force_reread(self) -> str:
        """Force re-read the token file (called on 401 errors).

        For string mode, this is a no-op — returns the same token.
        For file mode, re-reads the file immediately.
        Returns the token value (may be same as before if file unchanged).
        """
        if self._mode == "string":
            return self._token
        self._last_read_time = 0.0
        return self.access_token

    @property
    def token_file_path(self) -> Path | None:
        """Return the token file path (None for string mode)."""
        return self._token_file

    @property
    def _is_stale(self) -> bool:
        """Check if cached token should be refreshed (file mode only)."""
        if not self._token:
            return True
        return (time.monotonic() - self._last_read_time) > _REREAD_INTERVAL

    def _read_token_file(self) -> None:
        """Read and parse the token file."""
        assert self._token_file is not None

        try:
            raw = self._token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise TokenError(f"Token file not found: {self._token_file}")
        except OSError as e:
            raise TokenError(f"Cannot read token file {self._token_file}: {e}")

        if not raw:
            raise TokenError(f"Token file is empty: {self._token_file}")

        # Try JSON format first
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                token = data.get("access_token", "")
                if not token:
                    raise TokenError(f"Token file JSON has no access_token: {self._token_file}")
                self._token = token
            except json.JSONDecodeError:
                # Not valid JSON, treat as plain text
                self._token = raw
        else:
            # Plain text: entire content is the token
            self._token = raw

        self._last_read_time = time.monotonic()
        logger.debug("Token re-read from %s (%d chars)", self._token_file, len(self._token))
