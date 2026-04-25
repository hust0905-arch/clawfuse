# ClawFUSE

Drive Kit cloud storage mounted as a local FUSE filesystem for OpenClaw containers.

## Architecture Decision

**Drive Kit is the single source of truth.** All metadata is loaded at startup via BFS, file content is loaded on demand, writes are buffered and drained asynchronously. No local state is authoritative.

## Modules

| Module | Responsibility |
|--------|---------------|
| `config.py` | Frozen dataclass config. `from_env()` for legacy env vars, `from_file()` for JSON config. Validates at construction. |
| `token.py` | Dual-mode token: file-based (legacy, cached with 60s re-read) or string-based (from JSON config, immutable). |
| `client.py` | Drive Kit REST API wrapper. All requests include `containers=applicationData`. Handles multipart upload. |
| `dirtree.py` | In-memory directory tree built from Drive Kit file list. Path resolution via parent ID chain. |
| `cache.py` | LRU disk cache. Files stored as `{sha256}.data` + `.meta` JSON sidecar. Survives restarts. |
| `writebuf.py` | Write buffer with background drain. Files enqueued as `.buf` + `.meta`, uploaded by drain thread. |
| `fuse.py` | FUSE operations binding. Routes filesystem calls to DirTree/Cache/WriteBuffer. Single-writer per file. |
| `lifecycle.py` | Pre-start (token + folder resolution + DirTree load) and pre-destroy (flush pending writes). |
| `mount.py` | CLI entry point. `--config` for JSON mode, falls back to env vars. Signal handlers for graceful shutdown. |
| `exceptions.py` | `DriveKitError`, `TokenError`, `ConfigError`, `MountError`. |

## Development Commands

```bash
pip install -e ".[dev]"          # Install with dev dependencies
pytest tests/                    # Run all tests
pytest tests/ -k "not realapi"   # Skip real API tests
ruff check clawfuse/ tests/      # Lint
mypy clawfuse/                   # Type check
```

## Key Constraints

- **Token is read-only in file mode.** TokenManager never writes to the token file. Refresh must happen externally.
- **Single writer per file.** WriteBuffer enforces one active writer per file ID. Concurrent writes to the same file raise `WriteBufferError`.
- **Drive Kit API requires `containers=applicationData`** in every request. The `_params()` method on `DriveKitClient` adds this automatically.
- **Multipart upload format.** Create and update use Drive Kit multipart/form-data (metadata JSON + binary content).
- **Folder ID vs folder name.** Cloud folder is resolved at startup: `"applicationData"` is used directly, short names are looked up via `list_files`, long strings (20+ chars) are treated as folder IDs.
- **Config is frozen.** `Config` is a `@dataclass(frozen=True)`. To override, create a new Config instance with changed fields.
- **All files UTF-8 encoded.** Token files, config files, and source code use UTF-8 encoding.

## Testing

142 tests total (~83% coverage). Tests use mocked DriveKitClient — no real API calls in unit tests. Real API tests are in `tests/test_real_perf.py` marked with `pytest.mark.realapi` and require a valid token.
