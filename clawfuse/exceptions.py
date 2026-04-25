"""ClawFUSE custom exceptions."""


class ClawFUSEError(Exception):
    """Base exception for all ClawFUSE errors."""


class ConfigError(ClawFUSEError):
    """Configuration is missing or invalid."""


class TokenError(ClawFUSEError):
    """Token read or validation failed."""


class DriveKitError(ClawFUSEError):
    """Drive Kit API returned a non-success status."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Drive Kit API error {status_code}: {body[:200]}")


class CacheError(ClawFUSEError):
    """Cache I/O error."""


class SyncError(ClawFUSEError):
    """Write sync failed."""

    def __init__(self, file_id: str, attempts: int, message: str = "") -> None:
        self.file_id = file_id
        self.attempts = attempts
        super().__init__(f"Sync failed for {file_id} after {attempts} attempts: {message}")


class MountError(ClawFUSEError):
    """FUSE mount failed."""
