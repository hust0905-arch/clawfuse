"""ClawFUSE configuration — JSON config file + environment variables + frozen dataclass."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .exceptions import ConfigError

# Drive Kit API endpoints
API_BASE = "https://driveapis.cloud.huawei.com.cn/drive/v1"
UPLOAD_BASE = "https://drive.cloud.hicloud.com/upload/drive/v1"

# Folder MIME type
FOLDER_MIME = "application/vnd.huawei-apps.folder"

# Defaults
DEFAULT_MOUNT_POINT = "/mnt/drive"
DEFAULT_ROOT_FOLDER = "applicationData"
DEFAULT_CACHE_DIR = "/tmp/clawfuse-cache"
DEFAULT_CACHE_MAX_MB = 512
DEFAULT_CACHE_MAX_FILES = 500
DEFAULT_WRITE_BUF_DIR = "/tmp/clawfuse-writes"
DEFAULT_DRAIN_INTERVAL = 5.0
DEFAULT_DRAIN_MAX_RETRIES = 3
DEFAULT_TREE_REFRESH_TTL = 10.0
DEFAULT_LIST_PAGE_SIZE = 100
DEFAULT_HTTP_TIMEOUT = 30
DEFAULT_LOG_LEVEL = "INFO"


def _env(name: str, default: str | None = None) -> str | None:
    """Read environment variable."""
    return os.environ.get(name, default)


def _env_path(name: str, default: str | None = None) -> Path | None:
    """Read environment variable as Path."""
    val = _env(name, default)
    return Path(val) if val else None


def _env_int(name: str, default: int) -> int:
    """Read environment variable as int."""
    val = _env(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        raise ConfigError(f"Invalid integer for {name}: {val!r}")


def _env_float(name: str, default: float) -> float:
    """Read environment variable as float."""
    val = _env(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        raise ConfigError(f"Invalid float for {name}: {val!r}")


@dataclass(frozen=True)
class Config:
    """Immutable configuration loaded from JSON file or environment variables.

    Supports two modes:
    1. JSON config file via from_file() — token as string, cloud_folder by name
    2. Environment variables via from_env() — token from file (legacy)
    """

    # Token source (one of these must be set)
    token_file: Path | None = None
    token_string: str = ""

    # Config file path (set when loaded from JSON, enables token hot-reload)
    config_file_path: Path | None = None

    # Core mapping
    cloud_folder: str = DEFAULT_ROOT_FOLDER
    mount_point: str = DEFAULT_MOUNT_POINT

    # Resolved root folder ID (set at runtime after cloud_folder resolution)
    root_folder: str = DEFAULT_ROOT_FOLDER

    # Cache
    cache_dir: Path = field(default_factory=lambda: Path(DEFAULT_CACHE_DIR))
    cache_max_bytes: int = DEFAULT_CACHE_MAX_MB * 1024 * 1024
    cache_max_files: int = DEFAULT_CACHE_MAX_FILES

    # Write buffer
    write_buf_dir: Path = field(default_factory=lambda: Path(DEFAULT_WRITE_BUF_DIR))
    drain_interval: float = DEFAULT_DRAIN_INTERVAL
    drain_max_retries: int = DEFAULT_DRAIN_MAX_RETRIES

    # DirTree
    tree_refresh_ttl: float = DEFAULT_TREE_REFRESH_TTL
    list_page_size: int = DEFAULT_LIST_PAGE_SIZE

    # Network
    http_timeout: int = DEFAULT_HTTP_TIMEOUT

    # FUSE
    allow_other: bool = False
    nonempty: bool = False

    # Logging
    log_level: str = DEFAULT_LOG_LEVEL

    @classmethod
    def from_file(cls, path: Path) -> Config:
        """Create Config from a JSON config file.

        Required fields: token
        Optional fields: cloud_folder, mount_point, cache_dir, etc.
        """
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")

        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in config file {path}: {e}")
        except OSError as e:
            raise ConfigError(f"Cannot read config file {path}: {e}")

        # Token is required
        token = data.get("token", "").strip()
        if not token:
            raise ConfigError("Config file must contain a 'token' field")

        # Parse optional fields with defaults
        cache_max_mb = data.get("cache_max_mb", DEFAULT_CACHE_MAX_MB)

        cloud_folder = data.get("cloud_folder", DEFAULT_ROOT_FOLDER)

        return cls(
            token_file=None,
            token_string=token,
            config_file_path=path,
            cloud_folder=cloud_folder,
            mount_point=data.get("mount_point", DEFAULT_MOUNT_POINT),
            root_folder=cloud_folder if _looks_like_folder_id(cloud_folder) else DEFAULT_ROOT_FOLDER,
            cache_dir=Path(data.get("cache_dir", DEFAULT_CACHE_DIR)),
            cache_max_bytes=cache_max_mb * 1024 * 1024,
            cache_max_files=data.get("cache_max_files", DEFAULT_CACHE_MAX_FILES),
            write_buf_dir=Path(data.get("write_buf_dir", DEFAULT_WRITE_BUF_DIR)),
            drain_interval=data.get("drain_interval", DEFAULT_DRAIN_INTERVAL),
            drain_max_retries=data.get("drain_max_retries", DEFAULT_DRAIN_MAX_RETRIES),
            tree_refresh_ttl=data.get("tree_refresh_ttl", DEFAULT_TREE_REFRESH_TTL),
            list_page_size=data.get("list_page_size", DEFAULT_LIST_PAGE_SIZE),
            http_timeout=data.get("http_timeout", DEFAULT_HTTP_TIMEOUT),
            log_level=data.get("log_level", DEFAULT_LOG_LEVEL) or DEFAULT_LOG_LEVEL,
            allow_other=data.get("allow_other", False),
            nonempty=data.get("nonempty", False),
        )

    @classmethod
    def from_env(cls) -> Config:
        """Create Config from environment variables (legacy mode)."""
        token_file = _env_path("CLAWFUSE_TOKEN_FILE", "/run/secrets/access_token")
        if token_file is None:
            raise ConfigError("CLAWFUSE_TOKEN_FILE is required")

        cache_max_mb = _env_int("CLAWFUSE_CACHE_MAX_MB", DEFAULT_CACHE_MAX_MB)

        root_folder = _env("CLAWFUSE_ROOT_FOLDER", DEFAULT_ROOT_FOLDER) or DEFAULT_ROOT_FOLDER

        return cls(
            token_file=token_file,
            token_string="",
            cloud_folder=root_folder,
            mount_point=_env("CLAWFUSE_MOUNT_POINT", DEFAULT_MOUNT_POINT) or DEFAULT_MOUNT_POINT,
            root_folder=root_folder,
            cache_dir=_env_path("CLAWFUSE_CACHE_DIR", DEFAULT_CACHE_DIR) or Path(DEFAULT_CACHE_DIR),
            cache_max_bytes=cache_max_mb * 1024 * 1024,
            cache_max_files=_env_int("CLAWFUSE_CACHE_MAX_FILES", DEFAULT_CACHE_MAX_FILES),
            write_buf_dir=_env_path("CLAWFUSE_WRITE_BUF_DIR", DEFAULT_WRITE_BUF_DIR) or Path(DEFAULT_WRITE_BUF_DIR),
            drain_interval=_env_float("CLAWFUSE_DRAIN_INTERVAL", DEFAULT_DRAIN_INTERVAL),
            drain_max_retries=_env_int("CLAWFUSE_DRAIN_MAX_RETRIES", DEFAULT_DRAIN_MAX_RETRIES),
            tree_refresh_ttl=_env_float("CLAWFUSE_TREE_REFRESH_TTL", DEFAULT_TREE_REFRESH_TTL),
            list_page_size=_env_int("CLAWFUSE_LIST_PAGE_SIZE", DEFAULT_LIST_PAGE_SIZE),
            http_timeout=_env_int("CLAWFUSE_HTTP_TIMEOUT", DEFAULT_HTTP_TIMEOUT),
            log_level=_env("CLAWFUSE_LOG_LEVEL", DEFAULT_LOG_LEVEL) or DEFAULT_LOG_LEVEL,
            allow_other=bool(_env("CLAWFUSE_ALLOW_OTHER", "")),
            nonempty=bool(_env("CLAWFUSE_NONEMPTY", "")),
        )

    def validate(self) -> None:
        """Validate configuration at startup."""
        has_token = bool(self.token_string) or (self.token_file is not None)
        if not has_token:
            raise ConfigError("Either token_string or token_file must be set")
        if self.cache_max_bytes <= 0:
            raise ConfigError("cache_max_bytes must be positive")
        if self.cache_max_files <= 0:
            raise ConfigError("cache_max_files must be positive")
        if self.drain_interval <= 0:
            raise ConfigError("drain_interval must be positive")
        if self.list_page_size <= 0 or self.list_page_size > 100:
            raise ConfigError("list_page_size must be between 1 and 100")

    def ensure_dirs(self) -> None:
        """Create required directories if they don't exist."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.write_buf_dir.mkdir(parents=True, exist_ok=True)
        Path(self.mount_point).mkdir(parents=True, exist_ok=True)

    @property
    def needs_folder_resolution(self) -> bool:
        """Whether cloud_folder is a name that needs to be resolved to an ID."""
        return (
            self.cloud_folder != DEFAULT_ROOT_FOLDER
            and not _looks_like_folder_id(self.cloud_folder)
        )


def _looks_like_folder_id(value: str) -> bool:
    """Check if a value looks like a Drive Kit folder ID rather than a name.

    Drive Kit folder IDs are typically long alphanumeric strings (20+ chars).
    Folder names are short human-readable strings like 'workspace'.
    """
    if value == DEFAULT_ROOT_FOLDER:
        return True
    # IDs are typically 20+ characters, names are shorter
    return len(value) >= 20
