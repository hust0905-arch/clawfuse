# ClawFUSE

Drive Kit cloud storage FUSE mount for OpenClaw containers. Mounts Huawei Drive Kit `applicationData` as a local filesystem, providing transparent read/write access for containerized AI agents.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Container (OpenClaw)                   │
│                                                          │
│  Agent ──► /mnt/drive/  ──► ClawFUSE                     │
│                               │                          │
│                    ┌──────────┼──────────┐                │
│                    │          │          │                │
│                DirTree    Cache     WriteBuffer           │
│               (metadata)  (reads)    (writes)             │
│                    │          │          │                │
│                    └──────────┼──────────┘                │
│                               │                          │
│                         DriveKitClient                    │
│                               │                          │
└───────────────────────────────┼──────────────────────────┘
                                │  HTTPS
                                ▼
                     ┌─────────────────────┐
                     │   Drive Kit Cloud   │
                     │  (Huawei Cloud)     │
                     └─────────────────────┘
```

**Design principle:** Full metadata preload + lazy file content loading + async write drain. Metadata operations (ls, stat) hit memory only. File reads go through an LRU disk cache. Writes buffer locally and drain to Drive Kit in the background.

## Quick Start

### 1. Install

```bash
pip install -e ".[fuse]"
```

### 2. Create config file

Copy `clawfuse.json.example` to `clawfuse.json` and fill in your Drive Kit access token:

```json
{
  "token": "YOUR_ACCESS_TOKEN",
  "cloud_folder": "applicationData",
  "mount_point": "/mnt/drive"
}
```

### 3. Mount

```bash
clawfuse --config clawfuse.json
```

## Configuration

Create a `clawfuse.json` file (see `clawfuse.json.example`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `token` | string | **required** | Drive Kit OAuth access token |
| `cloud_folder` | string | `"applicationData"` | Cloud folder name or ID to mount |
| `mount_point` | string | `"/mnt/drive"` | Local FUSE mount point |
| `cache_dir` | string | `"/tmp/clawfuse-cache"` | Disk cache directory |
| `cache_max_mb` | int | `512` | Max cache size in MB |
| `cache_max_files` | int | `500` | Max cached files |
| `write_buf_dir` | string | `"/tmp/clawfuse-writes"` | Write buffer directory |
| `drain_interval` | float | `5.0` | Seconds between background uploads |
| `drain_max_retries` | int | `3` | Max retries per failed upload |
| `tree_refresh_ttl` | float | `10.0` | Seconds between DirTree refreshes |
| `list_page_size` | int | `200` | Drive Kit list API page size |
| `http_timeout` | int | `30` | HTTP request timeout in seconds |
| `log_level` | string | `"INFO"` | Logging level |

### Cloud folder

- `"applicationData"` — default container data folder (no resolution needed)
- A folder name (e.g. `"workspace"`) — resolved to ID at startup via `list_files`
- A folder ID (20+ character string) — used directly

## Performance

Based on real Drive Kit API measurements (2026-04-24, China mainland network):

| Operation | Latency | Notes |
|-----------|---------|-------|
| `getattr` / `readdir` | **< 0.02ms** | Pure memory — no API call |
| `read` (cache hit) | **~0.2ms** | Disk cache read |
| `read` (cache miss) | **0.8-1.5s** | Drive Kit download + cache fill |
| `write` | **< 100ms** | In-memory buffer, async upload |
| `create` / `mkdir` | **0.6-1.0s** | Drive Kit API call |
| Mount startup (100 files) | **~1s** | DirTree BFS preload |
| Mount startup (1000 files) | **~5s** | BFS is the bottleneck |

**Key insight:** Drive Kit has ~800ms fixed API overhead per call regardless of file size. Cache hit provides **3000-4000x** speedup over direct API access.

## Project Structure

```
clawfuse/
  __init__.py          # Package init
  cache.py             # LRU disk cache (ContentCache)
  client.py            # Drive Kit REST API client
  config.py            # Config dataclass (from_env / from_file)
  dirtree.py           # In-memory directory tree (DirTree)
  exceptions.py        # Custom exceptions
  fuse.py              # FUSE operations (ClawFUSE)
  lifecycle.py         # Pre-start / pre-destroy lifecycle
  mount.py             # CLI entry point
  token.py             # Token manager (file or string mode)
  writebuf.py          # Write buffer with async drain
tests/
  conftest.py          # Shared fixtures
  test_*.py            # Unit + perf tests
docs/
  ClawFUSE-架构设计说明书.md
  ClawFUSE-详细设计说明书.md
  ClawFUSE-性能测试报告.md
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/

# Run linter
ruff check clawfuse/ tests/

# Type check
mypy clawfuse/
```

## License

MIT
