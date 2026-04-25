# ClawFUSE 详细设计说明书

> 版本: 1.0 | 日期: 2026-04-24
> 基于架构设计说明书 v1.0

## 1. 概述

本文档基于 ClawFUSE 架构设计说明书，详细描述各模块的接口定义、数据结构、核心流程和实现细节。

### 1.1 系统范围

ClawFUSE 在 OpenClaw 容器内运行，将华为 Drive Kit 云存储挂载为本地文件系统。仅涉及单容器场景，不包含多设备同步、分布式锁、事件系统。

### 1.2 术语

参见架构设计说明书 §1.3 术语表。

### 1.3 参考文档

- ClawFUSE 架构设计说明书 v1.0
- 华为 Drive Kit REST API 文档
- MemexFS 详细设计说明书（模式参考）

## 2. 数据结构定义

### 2.1 Config（配置）

```python
@dataclass(frozen=True)
class Config:
    # 必需
    token_file: Path          # access_token 文件路径

    # 可选（有默认值）
    mount_point: str          # FUSE 挂载点，默认 "/mnt/drive"
    root_folder: str          # Drive Kit 根文件夹 ID，默认 "applicationData"
    cache_dir: Path           # 缓存目录，默认 "/tmp/clawfuse-cache"
    cache_max_bytes: int      # 最大缓存字节数，默认 536870912 (512MB)
    cache_max_files: int      # 最大缓存文件数，默认 500
    write_buf_dir: Path       # 写缓冲目录，默认 "/tmp/clawfuse-writes"
    drain_interval: float     # 写排空间隔秒，默认 5.0
    drain_max_retries: int    # 上传重试次数，默认 3
    tree_refresh_ttl: float   # 目录树刷新 TTL 秒，默认 10.0
    list_page_size: int       # Drive Kit list 分页大小，默认 200
    http_timeout: int         # HTTP 请求超时秒，默认 30
    log_level: str            # 日志级别，默认 "INFO"
```

**从环境变量映射**：

| 环境变量 | 字段 | 转换 |
|---------|------|------|
| `CLAWFUSE_TOKEN_FILE` | `token_file` | `Path(value)` |
| `CLAWFUSE_MOUNT_POINT` | `mount_point` | 直传 |
| `CLAWFUSE_ROOT_FOLDER` | `root_folder` | 直传 |
| `CLAWFUSE_CACHE_DIR` | `cache_dir` | `Path(value)` |
| `CLAWFUSE_CACHE_MAX_MB` | `cache_max_bytes` | `int(value) * 1024 * 1024` |
| `CLAWFUSE_CACHE_MAX_FILES` | `cache_max_files` | `int(value)` |
| `CLAWFUSE_WRITE_BUF_DIR` | `write_buf_dir` | `Path(value)` |
| `CLAWFUSE_DRAIN_INTERVAL` | `drain_interval` | `float(value)` |
| `CLAWFUSE_LOG_LEVEL` | `log_level` | 直传 |

### 2.2 FileMeta（文件元数据）

```python
@dataclass(frozen=True)
class FileMeta:
    id: str                   # Drive Kit 文件 ID
    name: str                 # 文件名
    is_dir: bool              # 是否为目录
    size: int                 # 文件大小（字节），目录为 0
    sha256: str               # SHA-256 哈希，目录为 ""
    parent_id: str            # 父目录 ID，根为 ""
    modified_time: str        # ISO 8601 时间戳
```

### 2.3 CacheEntry（缓存条目）

```python
@dataclass(frozen=True)
class CacheEntry:
    file_id: str              # Drive Kit 文件 ID
    path: str                 # 文件逻辑路径
    size: int                 # 文件大小
    sha256: str               # 内容 SHA-256
    last_access: float        # 最后访问时间戳（time.time()）
    disk_path: Path           # 磁盘上的 .content 文件路径
```

### 2.4 PendingWrite（待写入）

```python
@dataclass
class PendingWrite:
    file_id: str              # Drive Kit 文件 ID
    path: str                 # 文件逻辑路径
    content: bytes            # 文件内容
    sha256: str               # 内容 SHA-256
    queued_at: float          # 入队时间戳
    retry_count: int          # 已重试次数
    status: str               # "pending" | "uploading" | "failed"
```

### 2.5 异常层级

```
ClawFUSEError (base)
├── ConfigError           # 配置缺失或无效
├── TokenError            # Token 读取失败
├── DriveKitError         # Drive Kit API 错误
│   └── .status_code: int
│   └── .body: str
├── CacheError            # 缓存 I/O 错误
├── SyncError             # 写入同步失败
│   └── .file_id: str
│   └── .attempts: int
└── MountError            # FUSE 挂载失败
```

## 3. 模块详细设计

### 3.1 token.py — Token 管理

#### 接口

```python
class TokenManager:
    def __init__(self, token_file: Path) -> None:
        """初始化，设置 token 文件路径。"""

    @property
    def access_token(self) -> str:
        """获取有效的 access_token。
        - 首次调用时读取文件
        - 缓存到内存
        - 距上次读取超过 60 秒则重新读取
        """

    def force_reread(self) -> str:
        """强制重新读取 token 文件。
        - 在 401 错误时调用
        - 返回新的 token
        - 如果文件内容未变，返回相同 token
        """
```

#### Token 文件格式

**纯文本模式**（推荐）：
```
YAABhRMbW1...（一行，纯 access_token 字符串）
```

**JSON 模式**（可选）：
```json
{
  "access_token": "YAABhRMbW1...",
  "expires_at": 1713849600
}
```

#### 401 处理流程

```
DriveKitClient API 调用
    │
    ├─ 返回 401 Unauthorized
    │
    ▼
token.force_reread()
    │  重新读取 token 文件
    │
    ├─ token 变化 → 使用新 token 重试一次
    └─ token 未变 → 抛出 TokenError("token 无效且未更新")
```

### 3.2 client.py — Drive Kit 客户端

#### 接口

```python
class DriveKitClient:
    def __init__(self, token_manager: TokenManager, config: Config) -> None:
        """初始化，注入 token 管理器和配置。"""

    # ── 文件操作 ──

    def create_file(
        self,
        filename: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        parent_folder: str = "applicationData",
        fields: str = "id,fileName,sha256,size",
    ) -> dict:
        """创建文件。使用 multipart/related 上传。
        返回包含 fields 中指定字段的字典。"""

    def update_file(
        self,
        file_id: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        fields: str = "id,fileName,sha256,size",
    ) -> dict:
        """更新文件内容。使用 multipart/related PATCH。
        返回包含 fields 中指定字段的字典。"""

    def get_file(self, file_id: str, fields: str = "*") -> dict:
        """获取文件元数据。返回完整元数据字典。"""

    def download_file(self, file_id: str) -> bytes:
        """下载文件内容。返回原始字节。"""

    def delete_file(self, file_id: str) -> None:
        """删除文件（移入回收站）。"""

    # ── 文件夹操作 ──

    def create_folder(
        self,
        folder_name: str,
        parent_folder: str = "applicationData",
        fields: str = "id,fileName",
    ) -> dict:
        """创建文件夹。mimeType = FOLDER_MIME。"""

    # ── 列表操作 ──

    def list_files(
        self,
        parent_folder: str | None = None,
        page_size: int = 200,
        fields: str = "files(id,fileName,mimeType,sha256,size,parentFolder,modifiedTime),nextCursor",
        cursor: str | None = None,
    ) -> dict:
        """列出文件。返回 {"files": [...], "nextCursor": "..."}。
        如果 cursor 存在，继续分页。"""

    # ── 内部方法 ──

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """发送 HTTP 请求，401 时重试一次。"""

    @property
    def _auth_headers(self) -> dict[str, str]:
        """返回 Authorization header。"""
```

#### Multipart 上传格式

```
POST/PATCH {UPLOAD_BASE}/files[/{file_id}]
Content-Type: multipart/related; boundary={boundary}

--{boundary}
Content-Type: application/json; charset=UTF-8

{"fileName": "report.csv", "mimeType": "text/csv", "parentFolder": ["folderId"]}

--{boundary}
Content-Type: text/csv

（文件内容二进制数据）

--{boundary}--
```

### 3.3 dirtree.py — 目录树

#### 接口

```python
class DirTree:
    def __init__(self, client: DriveKitClient, root_folder: str, refresh_ttl: float = 10.0) -> None:
        """初始化，设置根文件夹和刷新 TTL。"""

    def refresh(self) -> None:
        """从 Drive Kit 加载全量文件元数据，构建目录树。
        - 分页拉取所有文件（pageSize=200）
        - 按 parentFolder 递归构建路径
        - 过滤隐藏文件（以 . 开头）
        """

    def resolve(self, path: str) -> FileMeta | None:
        """将路径解析为 FileMeta。
        如果 TTL 过期，先 refresh 再解析。
        路径格式："/data/subdir/file.txt"
        """

    def list_dir(self, path: str) -> list[str]:
        """列举目录下直接子项名称。
        路径 "/" 表示根目录。
        返回文件/文件夹名称列表。
        """

    def get_path(self, file_id: str) -> str | None:
        """file_id 反查路径。"""

    def add_entry(self, path: str, meta: FileMeta) -> None:
        """添加新文件/目录条目（create/mkdir 后调用）。"""

    def remove_entry(self, path: str) -> None:
        """移除条目（unlink/rmdir 后调用）。"""

    def move_entry(self, old_path: str, new_path: str) -> None:
        """移动/重命名条目。"""

    @property
    def file_count(self) -> int:
        """当前文件+目录总数。"""

    @property
    def last_refresh_time(self) -> float:
        """上次刷新时间戳。"""
```

#### 启动加载流程

```
DirTree.refresh()
    │
    ├─ 1. GET /files?parentFolder={root}&pageSize=200
    │     → 获取根目录第一页
    │
    ├─ 2. 如果有 nextCursor → 继续分页
    │     → 合并所有 files
    │
    ├─ 3. 识别子文件夹 → 对每个子文件夹递归 list_files
    │     → BFS 遍历所有层级
    │
    ├─ 4. 构建 _path_map: dict[str, FileMeta]
    │     path → FileMeta
    │     如 "/data/report.csv" → FileMeta(...)
    │
    ├─ 5. 构建 _id_map: dict[str, str]
    │     file_id → path
    │     如 "abc123" → "/data/report.csv"
    │
    └─ 6. 记录 last_refresh_time
```

#### 路径构建算法

```python
def _build_path(self, file_id: str, id_to_raw: dict) -> str:
    """从 file_id 递归构建完整路径。

    通过 parentFolder chain 向上追溯直到根目录，
    拼接各层文件夹名称形成完整路径。
    """
    parts = []
    current_id = file_id
    while current_id and current_id != self._root_folder:
        raw = id_to_raw.get(current_id)
        if not raw:
            break
        parts.append(raw["fileName"])
        parents = raw.get("parentFolder", [])
        current_id = parents[0]["id"] if parents else ""
    return "/" + "/".join(reversed(parts))
```

### 3.4 cache.py — 磁盘 LRU 缓存

#### 接口

```python
class ContentCache:
    def __init__(self, cache_dir: Path, max_bytes: int, max_files: int) -> None:
        """初始化缓存。
        - 创建 cache_dir（如不存在）
        - 启动时扫描已有 .meta 文件，重建 LRU 索引
        """

    def get(self, file_id: str) -> bytes | None:
        """获取缓存内容。
        - 查找 LRU 索引
        - 从磁盘读取 .content 文件
        - 更新 last_access，移动到 LRU 尾部
        - 返回 bytes，未命中返回 None
        """

    def put(self, file_id: str, path: str, content: bytes, sha256: str) -> None:
        """存入缓存。
        - 原子写入 .content 文件（先写 .tmp 再 rename）
        - 写入 .meta sidecar
        - 添加到 LRU 索引
        - 如超出 max_bytes，执行淘汰
        """

    def invalidate(self, file_id: str) -> None:
        """使缓存条目失效。
        - 从索引中移除
        - 删除 .content 和 .meta 文件
        """

    def contains(self, file_id: str) -> bool:
        """检查是否在缓存中。"""

    @property
    def total_bytes(self) -> int:
        """当前缓存总字节数。"""

    @property
    def entry_count(self) -> int:
        """当前缓存条目数。"""
```

#### 磁盘布局

```
{cache_dir}/
├── ab/                         # file_id 前两个字符作为子目录
│   ├── abcdef123456.content    # 原始文件字节
│   └── abcdef123456.meta       # JSON 元信息
├── 9f/
│   ├── 9f45678901234.content
│   └── 9f45678901234.meta
└── ...
```

#### .meta 文件格式

```json
{
  "file_id": "abcdef123456",
  "path": "/data/report.csv",
  "size": 12345,
  "sha256": "a1b2c3d4...",
  "last_access": 1713849600.123
}
```

#### LRU 淘汰算法

```python
# 内部数据结构
_lru: OrderedDict[str, CacheEntry]  # file_id → entry，按访问顺序
_total_bytes: int

def _evict_if_needed(self) -> None:
    """淘汰超出预算的条目。"""
    while self._total_bytes > self._max_bytes and self._lru:
        # OrderedDict.popitem(last=False) 弹出最早（最少使用）的条目
        file_id, entry = self._lru.popitem(last=False)
        # 删除磁盘文件
        entry.disk_path.unlink(missing_ok=True)
        meta_path = entry.disk_path.with_suffix(".meta")
        meta_path.unlink(missing_ok=True)
        self._total_bytes -= entry.size
```

#### 原子写入

```python
def _write_content(self, path: Path, content: bytes) -> None:
    """原子写入文件内容。"""
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_bytes(content)
    tmp_path.rename(path)  # 原子操作
```

### 3.5 writebuf.py — 写缓冲

#### 接口

```python
class WriteBuffer:
    def __init__(self, client: DriveKitClient, buffer_dir: Path, drain_interval: float = 5.0) -> None:
        """初始化写缓冲。
        - 创建 buffer_dir（如不存在）
        - 启动时扫描 .buf 文件，恢复 pending writes
        """

    def enqueue(self, file_id: str, path: str, content: bytes, sha256: str) -> None:
        """将写入加入队列。
        1. 创建 PendingWrite
        2. 内容写入 buffer_dir/{file_id}.buf（crash safety）
        3. 添加到内存队列
        """

    def start_drain(self) -> None:
        """启动后台 drain 线程。"""

    def stop_drain(self) -> None:
        """停止后台 drain 线程。
        等待当前上传完成。"""

    def flush_all(self, timeout: float = 120.0) -> FlushResult:
        """同步排空所有 pending writes。
        在 pre-destroy 中调用。
        阻塞直到全部完成或超时。"""

    def get_pending(self, file_id: str) -> PendingWrite | None:
        """获取指定文件的 pending write。"""

    @property
    def pending_count(self) -> int:
        """待上传文件数。"""

    @property
    def has_pending(self) -> bool:
        """是否有待上传文件。"""

@dataclass(frozen=True)
class FlushResult:
    total: int
    succeeded: int
    failed: int
    errors: list[str]
```

#### 后台 drain 线程

```python
def _drain_loop(self) -> None:
    """后台线程主循环。"""
    while not self._stop_event.is_set():
        self._stop_event.wait(self._drain_interval)
        if self._stop_event.is_set():
            break
        self._drain_one_batch()

def _drain_one_batch(self) -> None:
    """排空一批 pending writes。"""
    pending = [w for w in self._queue.values() if w.status == "pending"]
    for write in pending:
        if self._stop_event.is_set():
            break
        self._upload_one(write)

def _upload_one(self, write: PendingWrite) -> bool:
    """上传单个文件。"""
    try:
        write.status = "uploading"
        if write.file_id:  # 更新已有文件
            self._client.update_file(write.file_id, write.content)
        else:  # 新建文件
            result = self._client.create_file(
                filename=Path(write.path).name,
                content=write.content,
                parent_folder=self._get_parent_id(write.path),
            )
            write.file_id = result["id"]
        # 上传成功，删除 .buf 文件
        self._remove_buf_file(write)
        del self._queue[write.file_id]
        return True
    except DriveKitError:
        write.retry_count += 1
        if write.retry_count >= self._max_retries:
            write.status = "failed"
        else:
            write.status = "pending"  # 下次重试
        return False
```

#### flush_all 流程

```
flush_all(timeout=120)
    │
    ├─ 1. 停止后台 drain 线程
    │
    ├─ 2. 获取所有 pending writes
    │
    ├─ 3. 逐个同步上传
    │     ├─ 成功 → 删除 .buf，计数 succeeded
    │     ├─ 失败 → 重试最多 max_retries 次
    │     └─ 超时 → 记录错误，计数 failed
    │
    └─ 4. 返回 FlushResult(total, succeeded, failed, errors)
```

### 3.6 fuse.py — FUSE 操作

#### 接口

```python
class ClawFUSE(logging.WarningsModule):
    """FUSE 文件系统操作。"""

    def __init__(
        self,
        client: DriveKitClient,
        dirtree: DirTree,
        cache: ContentCache,
        writebuf: WriteBuffer,
        config: Config,
    ) -> None: ...

    # ── 文件操作 ──

    def getattr(self, path: str, fh: int | None = None) -> dict:
        """获取文件/目录属性。
        返回 FuseStat dict（st_mode, st_size, st_nlink, st_uid, st_gid, st_mtime, st_atime, st_ctime）。
        """

    def readdir(self, path: str, fh: int) -> list[str]:
        """列出目录内容。
        返回 [".", ".."] + 子项名称列表。
        """

    def open(self, path: str, flags: int) -> int:
        """打开文件。
        - 分配文件句柄（_next_fh）
        - 注册到 _fh_map[fh] = file_id
        - 对于写模式，初始化 _content_map[fh] = b""
        """

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        """读取文件内容。
        1. 查找 file_id
        2. 如果有内存缓冲（写入中）→ 从缓冲返回
        3. 查缓存 → 命中返回
        4. 下载 → 缓存 → 返回
        """

    def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
        """写入文件内容。
        1. 扩展/修改内存缓冲
        2. 标记为脏
        3. 返回 len(data)
        """

    def create(self, path: str, mode: int) -> int:
        """创建新文件。
        1. 解析父目录 + 文件名
        2. client.create_file(filename, b"")
        3. dirtree.add_entry(path, meta)
        4. 分配 fh
        """

    def flush(self, path: str, fh: int) -> None:
        """刷新文件。
        如果 fh 是脏的，enqueue 到 WriteBuffer。
        """

    def release(self, path: str, fh: int) -> None:
        """关闭文件。
        清理 _fh_map, _content_map, _dirty。
        """

    def unlink(self, path: str) -> None:
        """删除文件。
        1. client.delete_file(file_id)
        2. cache.invalidate(file_id)
        3. dirtree.remove_entry(path)
        """

    def truncate(self, path: str, length: int, fh: int | None = None) -> None:
        """截断文件。"""

    # ── 目录操作 ──

    def mkdir(self, path: str, mode: int) -> None:
        """创建目录。
        1. client.create_folder(name, parent)
        2. dirtree.add_entry(path, meta)
        """

    def rmdir(self, path: str) -> None:
        """删除空目录。
        1. client.delete_file(folder_id)
        2. dirtree.remove_entry(path)
        """

    def rename(self, old_path: str, new_path: str) -> None:
        """重命名/移动。
        1. client.update_metadata(file_id, fileName=new_name, parentFolder=[new_parent])
        2. dirtree.move_entry(old_path, new_path)
        """

    # ── 生命周期 ──

    def destroy(self, private_data: int) -> None:
        """FUSE 卸载回调。同步排空所有脏数据。"""

    # ── no-op 操作 ──

    def chmod(self, path: str, mode: int) -> None: ...
    def chown(self, path: str, uid: int, gid: int) -> None: ...
    def utimens(self, path: str, times: tuple | None = None) -> None: ...
    def access(self, path: str, amode: int) -> None: ...
    def statfs(self, path: str) -> dict: ...

    # ── 挂载入口 ──

    def mount(self, mountpoint: str, foreground: bool = False) -> None:
        """挂载 FUSE 文件系统。"""
```

#### 内部状态

```python
_fh_map: dict[int, str]          # fh → file_id
_content_map: dict[int, bytes]   # fh → 内存内容缓冲（写入中）
_dirty: set[int]                 # 有未写入数据的文件句柄
_next_fh: int = 1                # 文件句柄计数器
```

### 3.7 lifecycle.py — 生命周期管理

#### 接口

```python
@dataclass(frozen=True)
class MountResult:
    success: bool
    mount_point: str
    file_count: int
    load_time_seconds: float
    error: str = ""

@dataclass(frozen=True)
class SyncResult:
    files_synced: int
    files_failed: int
    errors: list[str]
    sync_time_seconds: float

@dataclass(frozen=True)
class StatusReport:
    mounted: bool
    mount_point: str
    file_count: int
    cache_entries: int
    cache_bytes: int
    pending_writes: int
    uptime_seconds: float

class LifecycleManager:
    def __init__(self, config: Config) -> None:
        """初始化所有组件（不启动）。"""

    def pre_start(self) -> MountResult:
        """容器启动前调用。组装并启动所有组件。"""

    def pre_destroy(self, timeout: float = 120.0) -> SyncResult:
        """容器销毁前调用。同步所有写入，卸载 FUSE。"""

    def status(self) -> StatusReport:
        """当前状态。"""

    @property
    def is_mounted(self) -> bool: ...
```

### 3.8 mount.py — CLI 入口

```python
def main() -> None:
    """CLI 入口点。

    1. 解析命令行参数（--mount-point, --token-file, --foreground 等）
    2. 从环境变量 + CLI 参数创建 Config
    3. lifecycle = LifecycleManager(config)
    4. result = lifecycle.pre_start()
    5. 注册信号处理：SIGTERM → lifecycle.pre_destroy() → sys.exit()
    6. 等待直到卸载
    """
```

## 4. 核心流程

### 4.1 启动挂载流程

```
LifecycleManager.pre_start()
    │
    ├─ [0-50ms]   1. 创建 Config
    │               验证环境变量
    │               创建 cache_dir, buffer_dir
    │
    ├─ [50-200ms]  2. 创建 TokenManager
    │               读取 token 文件
    │               验证 token 非空
    │
    ├─ [200ms-3s]  3. 创建 DirTree + refresh()
    │               分页拉取 Drive Kit 文件列表
    │               构建路径树
    │               N 文件 ≈ ceil(N/200) 页 × ~300ms/页
    │
    ├─ [3-3.1s]    4. 创建 ContentCache
    │               扫描 cache_dir 重建 LRU 索引
    │               恢复上次缓存
    │
    ├─ [3.1-3.2s]  5. 创建 WriteBuffer
    │               扫描 buffer_dir 恢复 pending writes
    │
    ├─ [3.2-3.5s]  6. 创建 ClawFUSE + mount()
    │               FUSE 线程启动
    │
    ├─ [3.5-3.6s]  7. WriteBuffer.start_drain()
    │               后台线程启动
    │
    └─ 返回 MountResult(success=True, file_count=N, load_time_seconds=~3s)
```

### 4.2 Read 流程

```
Agent: fd = open("/data/report.csv", O_RDONLY)
    │
    ▼ FUSE.open()
    │  dirtree.resolve("/data/report.csv") → FileMeta(id="abc123", ...)
    │  分配 fh=1, _fh_map[1]="abc123"
    │  返回 fh=1
    │
Agent: read(fd, buf, 4096)
    │
    ▼ FUSE.read(path="/data/report.csv", size=4096, offset=0, fh=1)
    │  file_id = _fh_map[1] = "abc123"
    │
    │  1. 检查 _content_map → 没有（非写入模式）
    │  2. cache.get("abc123")
    │     ├─ 命中 → content = 缓存 bytes
    │     └─ 未命中 → content = client.download("abc123")
    │                 → cache.put("abc123", path, content, sha256)
    │  3. 返回 content[0:4096]
    │
Agent: close(fd)
    │
    ▼ FUSE.release(path, fh=1)
    │  清理 _fh_map[1], 无脏数据
```

### 4.3 Write 流程

```
Agent: fd = open("/data/output.json", O_WRONLY|O_CREAT)
    │
    ▼ FUSE.create(path="/data/output.json", mode=0o644)
    │  parent = dirtree.resolve("/data") → FileMeta(id="folder123", is_dir=True)
    │  result = client.create_file("output.json", b"", parent="folder123")
    │  meta = FileMeta(id="new123", name="output.json", ...)
    │  dirtree.add_entry("/data/output.json", meta)
    │  _fh_map[2] = "new123", _content_map[2] = b""
    │  返回 fh=2
    │
Agent: write(fd, '{"result": 42}', 14)
    │
    ▼ FUSE.write(path, data=b'{"result": 42}', offset=0, fh=2)
    │  _content_map[2] = b'{"result": 42}'
    │  _dirty.add(2)
    │  返回 14
    │
Agent: close(fd)
    │
    ▼ FUSE.flush(path, fh=2)
    │  fh=2 is dirty → writebuf.enqueue("new123", path, content, sha256)
    │  → .buf 文件写入磁盘
    │
    ▼ FUSE.release(path, fh=2)
    │  清理 _fh_map, _content_map, _dirty
    │
    ▼ [5秒后] 后台 drain 线程
    │  client.update_file("new123", content)
    │  成功 → 删除 .buf 文件
```

### 4.4 Pre-destroy 同步流程

```
SIGTERM 信号 或 lifecycle.pre_destroy() 调用
    │
    ▼ LifecycleManager.pre_destroy(timeout=120)
    │
    ├─ 1. writebuf.stop_drain()
    │     等待后台线程停止
    │
    ├─ 2. result = writebuf.flush_all(timeout * 0.8)
    │     逐个同步上传所有 pending writes
    │     ┌─ 上传成功 → 删除 .buf，succeeded++
    │     ├─ 上传失败 → 重试 3 次
    │     └─ 全部失败 → 保留 .buf，failed++
    │
    ├─ 3. 如果有失败的 → 重试一次
    │
    ├─ 4. 卸载 FUSE
    │
    └─ 返回 SyncResult(files_synced=N, files_failed=0, ...)
```

## 5. 错误处理策略

| 错误场景 | 处理方式 |
|---------|---------|
| Token 文件不存在 | 启动失败，ConfigError |
| Token 无效（401） | 重新读取 token 文件 → 重试一次 → 仍失败则 TokenError |
| Drive Kit API 5xx | 重试 3 次（指数退避）→ 仍失败则 DriveKitError |
| Drive Kit API 4xx（非 401） | 记录错误，返回 EIO（读）或缓存失败（写） |
| 网络超时 | 读取返回 EIO；写入缓存在本地等待 drain |
| 缓存磁盘满 | LRU 淘汰至最低 → 仍满则返回 ENOSPC |
| .buf 文件损坏 | 跳过，记录警告 |
| FUSE getattr 路径不存在 | 返回 -ENOENT |
| FUSE readdir 路径不存在 | 返回 -ENOENT |

## 6. 性能关键路径分析

### 6.1 启动性能

瓶颈在 Drive Kit list API 分页：

| 文件数 | API 页数（pageSize=200） | 预估耗时 |
|--------|-------------------------|---------|
| 200 | 1 | ~0.3s |
| 1000 | 5 | ~1.5s |
| 5000 | 25 | ~7.5s |
| 10000 | 50 | ~15s |

**优化空间**：
- 使用 `queryParam` 过滤不需要的文件类型
- 并行请求不同子文件夹（如果 API 允许）
- 增量刷新（仅同步变化部分）— 未来优化

### 6.2 读取性能

| 场景 | 延迟来源 | 预估 |
|------|---------|------|
| 缓存命中 | 磁盘 I/O | <10ms |
| 缓存未命中（小文件 <1MB） | Drive Kit API + 网络传输 | 100-500ms |
| 缓存未命中（大文件 100MB） | Drive Kit API + 网络传输 | 1-10s（取决于带宽） |

### 6.3 写入性能

| 操作 | 延迟 |
|------|------|
| FUSE.write()（内存缓冲） | <1ms |
| FUSE.flush()（enqueue + 磁盘写入） | <5ms |
| 实际上传（drain 线程，异步） | 不影响 Agent |

### 6.4 内存占用

| 组件 | 内存占用 |
|------|---------|
| DirTree（1000 文件） | ~200KB（每个 FileMeta ~200 bytes） |
| Cache 索引 | 忽略不计（OrderedDict 引用） |
| FUSE 文件句柄 | 每个 <1KB |
| 文件内容缓冲 | 仅写入中的文件，读后释放 |

总内存占用（不含缓存文件内容）：约 1-5MB。文件内容走磁盘缓存，不占内存。
