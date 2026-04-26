# ClawFUSE 详细设计说明书

> 版本: 2.0 | 日期: 2026-04-26
> 基于: ClawFUSE 架构设计说明书 v2.0

---

## 1. 概述

本文档详细描述 ClawFUSE 各模块的接口定义、数据结构、核心算法和实现细节，作为开发参考。

## 2. 数据结构

### 2.1 Config

```python
@dataclass(frozen=True)
class Config:
    # 令牌（二选一）
    token_file: Path | None        # 文件模式：access_token 文件路径
    token_string: str | None       # 字符串模式：access_token 直接值（JSON 配置）

    # 挂载
    mount_point: str               # FUSE 挂载点，默认 "/mnt/drive"
    cloud_folder: str              # 云端文件夹，默认 "applicationData"
    root_folder: str               # 解析后的文件夹 ID（内部使用）

    # 缓存
    cache_dir: Path                # 默认 "/tmp/clawfuse-cache"
    cache_max_bytes: int           # 默认 536870912 (512MB)
    cache_max_files: int           # 默认 500

    # 写缓冲
    write_buf_dir: Path            # 默认 "/tmp/clawfuse-writes"
    drain_interval: float          # 默认 5.0s
    drain_max_retries: int         # 默认 3

    # 元数据
    tree_refresh_ttl: float        # 默认 10.0s
    list_page_size: int            # 默认 100（API 上限 100）

    # 网络
    http_timeout: int              # 默认 30s
    log_level: str                 # 默认 "INFO"
```

初始化方式: `from_env()`（环境变量）/ `from_file(path)`（JSON 配置文件）。构造时验证所有字段。

`cloud_folder` 解析规则:
- `"applicationData"` → 启动时通过 `_discover_application_data_root()` 发现真实根 ID
- 文件夹名称 → 发现根 ID 后 `list_files` 查找同名文件夹
- 文件夹 ID（≥ 20 字符）→ 直接使用

### 2.2 FileMeta

```python
@dataclass(frozen=True)
class FileMeta:
    id: str                   # Drive Kit 文件 ID
    name: str                 # 文件名
    is_dir: bool              # 是否目录
    size: int                 # 字节数（目录为 0）
    sha256: str               # SHA-256（目录为 ""）
    parent_id: str            # 父目录 ID（根为 ""）
    modified_time: str        # ISO 8601
```

### 2.3 CacheEntry

```python
@dataclass(frozen=True)
class CacheEntry:
    file_id: str
    path: str
    size: int
    sha256: str
    last_access: float        # time.time()
    disk_path: Path
```

### 2.4 PendingWrite

```python
@dataclass
class PendingWrite:
    file_id: str
    path: str
    content: bytes
    sha256: str
    queued_at: float
    retry_count: int
    status: str               # "pending" | "uploading" | "failed"
```

### 2.5 异常层级

```
DriveKitError(status_code, body)  # Drive Kit API 错误
TokenError                        # Token 读取/验证失败
ConfigError                       # 配置缺失或无效
MountError                        # FUSE 挂载失败
WriteBufferError                  # 写缓冲冲突（如重复写入同一文件）
```

## 3. 模块设计

### 3.1 token.py — 令牌管理

**双模式设计**:

| 模式 | 工厂方法 | 特性 |
|------|---------|------|
| 文件模式 | `from_file(path)` | 60 秒缓存重读；401 时 force_reread()；外部负责更新文件 |
| 字符串模式 | `from_string(token)` | 不可变；来自 JSON 配置；force_reread() 抛出 TokenError |

```python
class TokenManager:
    @property
    def access_token(self) -> str:
        """文件模式：距上次读取 > 60s 则重读文件。字符串模式：直接返回。"""

    def force_reread(self) -> str:
        """强制重读。文件模式：返回文件内容。字符串模式：抛出 TokenError。"""
```

401 处理: DriveKitClient 调用 force_reread() → token 变化则重试一次 → 未变则抛出 TokenError。

### 3.2 client.py — Drive Kit REST API 客户端

**所有请求自动附带**: `containers=applicationData`

```python
@staticmethod
def _params(**extra):
    return {"containers": "applicationData", **extra}
```

#### 接口定义

| 方法 | HTTP | 说明 |
|------|------|------|
| `create_file(filename, content, parent_folder)` | POST multipart/related | 创建文件 |
| `update_file(file_id, content)` | PATCH multipart/related | 更新文件内容 |
| `get_file(file_id)` | GET | 获取元数据 |
| `download_file(file_id)` | GET `?form=content` | 下载文件内容 |
| `delete_file(file_id)` | DELETE | 删除（移入回收站） |
| `create_folder(name, parent_folder)` | POST JSON | 创建文件夹 |
| `update_metadata(file_id, **meta)` | PATCH JSON | 更新元数据（fileName, parentFolder） |
| `list_files(parent_folder, page_size, cursor)` | GET | 列出文件（queryParam 过滤） |
| `list_all_files(root_folder)` | GET × N | BFS 递归列出全部（legacy） |

#### queryParam 过滤

```python
def list_files(self, parent_folder=None, page_size=100, cursor=None):
    p = self._params(pageSize=str(page_size))
    if parent_folder:
        p["queryParam"] = f"'{parent_folder}' in parentFolder"  # Google Drive 风格
    if cursor:
        p["pageCursor"] = cursor
```

- 不传 `parent_folder` → 返回容器内全部文件（不区分层级）
- 传 `parent_folder` → 仅返回该目录的直系子项

#### parentFolder 字段兼容

API 返回的 `parentFolder` 可能是两种格式，代码自动兼容:

```python
parent_id = (parents[0]["id"] if isinstance(parents[0], dict) else parents[0]) if parents else ""
```

#### 401 自动重试

所有 API 调用包裹在 `_retry_on_401` 中: 调用 → 401 → force_reread → 重试一次 → 仍 401 则抛出。

### 3.3 dirtree.py — 目录树

**三级加载策略**:

| 方法 | 触发方 | 用途 | 延迟 |
|------|--------|------|------|
| `load_dir(dir_id)` | ensure_loaded / background_full_load | 加载单个目录的直系子项 | ~800ms |
| `ensure_loaded(dir_path)` | FUSE getattr/readdir | 从根逐级加载到目标路径 | 0.8s × 层数 |
| `background_full_load(8)` | LifecycleManager 后台线程 | 8 线程 BFS 加载全部目录 | ~20s / 1110 目录 |

#### 并发模型

```
                          ┌── _loaded_dirs: set[str]  ── 已加载目录 ID
DirTree._lock (Mutex) ────┤
                          ├── _loading: set[str]      ── 正在加载的目录 ID
                          └── _load_condition          ── wait/notify 协调

load_dir(dir_id):
  fast path:  dir_id in _loaded_dirs → return (无锁，GIL set 查询)
  slow path:  acquire _lock
              ├─ double-check _loaded_dirs
              ├─ dir_id in _loading → Condition.wait()（零 CPU）
              └─ add to _loading → release lock → API 调用
                  → acquire lock → remove from _loading → notify_all()
```

同一目录只加载一次。并发请求同一目录时，先到者执行 API 调用，后到者 Condition.wait() 等待完成。

#### _load_dir_from_api 流程

```
API: list_files(parent_folder=dir_id) → queryParam='{dir_id}' in parentFolder
  │
  ├─ 分页: while nextCursor → 继续请求（防重复 cursor）
  │
  ├─ 遍历子项:
  │   ├── 跳过隐藏文件（.开头）和空名称
  │   ├── 兼容 parentFolder 字段格式（str/dict）
  │   ├── 解析完整路径（_resolve_path_for）
  │   └── 更新 _path_map, _id_map, _children_map
  │
  └─ _loaded_dirs.add(dir_id)
```

#### 路径解析算法

从 parentFolder 链向上追溯到根目录，拼接各层名称:

```python
def _resolve_path_for(item_id, parent_id, name, cache):
    if not parent_id or parent_id == root_folder:
        return "/" + name                    # 根级文件
    parent_path = resolve_cached(parent_id)  # 查缓存 + 已有 path_map
    return parent_path + "/" + name
```

使用 `path_cache: dict[str, str]` 缓存已解析路径，避免重复计算。

#### 核心索引

| 索引 | 类型 | 用途 |
|------|------|------|
| `_path_map` | `dict[str, FileMeta]` | 路径 → 元数据（getattr 查询） |
| `_id_map` | `dict[str, str]` | 文件 ID → 路径（反向查找） |
| `_children_map` | `dict[str, list[FileMeta]]` | 目录 ID → 子项列表（readdir 查询） |

### 3.4 cache.py — LRU 磁盘缓存

**磁盘布局**:

```
{cache_dir}/
├── ab/
│   ├── abcdef123.data       # 文件内容
│   └── abcdef123.meta       # JSON {file_id, path, size, sha256, last_access}
└── ...
```

文件 ID 前两个字符作为子目录，避免单目录文件过多。

**LRU 实现**: `OrderedDict[str, CacheEntry]`，访问时 `move_to_end()`，淘汰时 `popitem(last=False)` 弹出最久未访问。

**原子写入**: 先写 `.tmp` 临时文件，再 `rename` 为目标文件，防止写入中断导致数据损坏。

**启动恢复**: 扫描 `cache_dir` 下所有 `.meta` 文件，重建 LRU 索引。上次缓存自动可用。

### 3.5 writebuf.py — 写缓冲 + 异步上传

**数据流**:

```
FUSE.flush()
  └── enqueue(file_id, path, content, sha256)
        ├── .buf 文件写入（crash safety）
        ├── .meta 文件写入
        └── 加入内存队列

drain 线程（每 drain_interval 秒）
  └── 遍历队列
        ├── status == "pending" → 上传
        │   ├── file_id 存在 → client.update_file()
        │   └── file_id 为空 → client.create_file()
        ├── 成功 → 删除 .buf/.meta，移出队列
        └── 失败 → retry_count++，超过 max_retries 则标记 "failed"

pre_destroy()
  └── flush_all(timeout)
        └── stop_drain → 同步上传全部 pending → 返回 FlushResult
```

**单写者保证**: 同一 file_id 只允许一个活跃写入，重复写入抛出 `WriteBufferError`。

### 3.6 fuse.py — FUSE 操作

**核心设计**: 所有 `getattr`/`readdir` 调用前先 `ensure_loaded`，保证所需元数据已加载。

```python
def getattr(self, path, fh=None):
    if path == "/":
        return _dir_stat()
    parent = PurePosixPath(path).parent
    self._dirtree.ensure_loaded(str(parent))   # ← 懒加载
    meta = self._dirtree.resolve(path)
    if meta is None:
        raise FuseOSError(ENOENT)
    return _dir_stat() if meta.is_dir else _file_stat(meta.size)

def readdir(self, path, fh):
    self._dirtree.ensure_loaded(path)           # ← 懒加载
    return [".", ".."] + self._dirtree.list_dir(path)
```

**写入缓冲**: 使用 `bytearray` 而非 `bytes`，切片赋值 `buf[offset:offset+len] = data` 避免大文件 O(n²) 拷贝。

**三级读取查找**: FUSE.read → 写入缓冲 → Cache → download。

```python
def read(self, path, size, offset, fh):
    file_id = self._fh_map[fh]
    if fh in self._content_map:                 # 1. 写入中的缓冲
        return self._content_map[fh][offset:offset+size]
    content = self._cache.get(file_id)          # 2. 磁盘缓存
    if content is None:
        content = self._client.download_file(file_id)  # 3. API 下载
        self._cache.put(file_id, path, content, sha256)
    return content[offset:offset+size]
```

**flush 语义**: 脏数据同时写入 WriteBuffer（异步上传）和 Cache（后续读可见）。

**文件句柄管理**:

| 状态 | 类型 | 说明 |
|------|------|------|
| `_fh_map` | `dict[int, str]` | fh → file_id |
| `_content_map` | `dict[int, bytearray]` | fh → 写入缓冲（仅写入模式） |
| `_dirty` | `set[int]` | 有未写入数据的 fh |

### 3.7 lifecycle.py — 生命周期编排

**pre_start() 编排**:

```
1. ensure_dirs()
2. TokenManager 创建
3. DriveKitClient 创建
4. _resolve_root_folder()
   ├─ "applicationData" → _discover_application_data_root()
   ├─ 文件夹名称 → discover root → list_files 查找
   └─ 文件夹 ID → 直接使用
5. DirTree(client, root_folder)
6. 启动后台 BFS 线程（daemon，不阻塞）
7. ContentCache(cache_dir, max_bytes, max_files)
8. WriteBuffer(client, buffer_dir, ...)
9. writebuf.start_drain()
→ 返回 MountResult
```

**_discover_application_data_root()**:

```python
result = client.list_files(parent_folder="applicationData", page_size=10)
# queryParam='applicationData' in parentFolder → 列出根级文件
# 根级文件的 parentField 包含真实根 ID
parents = extract_parent_ids(result["files"])
return parents.pop() if parents else "applicationData"  # 回退
```

### 3.8 mount.py — CLI 入口

```bash
clawfuse --config clawfuse.json    # JSON 配置（推荐）
clawfuse                            # 环境变量（传统模式）
```

信号处理: SIGTERM → lifecycle.pre_destroy() → sys.exit(0)。

## 4. 时序图

### 4.1 启动到 Agent 首次读取

```
编排器        Lifecycle      DirTree(bg)     FUSE          Agent
  │              │               │             │              │
  │──create──────▶│               │             │              │
  │              │──init all─────▶│             │              │
  │              │──bg thread────▶│             │              │
  │              │──mount──────────────────────▶│              │
  │              │◀──ready───────│             │              │
  │              │                             │◀──ls /drive/─│
  │              │                             │──getattr("/")─│
  │              │                             │──ensure_loaded("/")
  │              │◀──load_dir(root)────────────│              │
  │              │                             │──readdir("/")─│
  │              │                             │──response─────│──────────────▶│
  │              │                             │◀──read file───│
  │              │◀──download + cache──────────│              │
  │              │                             │──data───────────────────────▶│
  │              │               │             │              │
  │              │   ─ ─ ─ BFS loading ─ ─ ─ ▶│              │
  │              │   ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ▶│              │
```

### 4.2 Agent 写入到持久化

```
Agent          FUSE           WriteBuffer       drain          Drive Kit
  │              │               │               │               │
  │──open(W)────▶│               │               │               │
  │──write──────▶│ buf[offset:]  │               │               │
  │──close──────▶│               │               │               │
  │              │──flush────────▶│               │               │
  │              │  enqueue()    │               │               │
  │              │  write .buf   │               │               │
  │              │  cache.put()  │               │               │
  │◀─────────────│               │               │               │
  │  (< 1ms)     │               │               │               │
  │              │               │               │               │
  │              │               │  (5s later)   │               │
  │              │               │──drain batch──▶│               │
  │              │               │               │──update_file──▶│
  │              │               │               │◀──200 OK──────│
  │              │               │               │──delete .buf──│
  │              │               │               │               │
```

### 4.3 容器销毁

```
编排器        Lifecycle       WriteBuffer      Drive Kit
  │              │               │               │
  │──SIGTERM────▶│               │               │
  │              │──flush_all───▶│               │
  │              │               │──upload each──▶│
  │              │               │◀──200─────────│
  │              │               │──delete .buf──│
  │              │◀──result──────│               │
  │◀──exit───────│               │               │
```

## 5. 错误处理矩阵

| 错误 | 来源 | 检测方式 | 处理 |
|------|------|----------|------|
| Token 文件缺失 | Config | 启动验证 | ConfigError，启动失败 |
| Token 无效 | Drive Kit | 401 响应 | force_reread → 重试 → TokenError |
| API 5xx | Drive Kit | status_code | drain 重试；读返回 EIO |
| API 4xx（非 401）| Drive Kit | status_code | 记录日志；返回 EIO/保留 .buf |
| 网络超时 | HTTP | timeout | 读返回 EIO；写缓存在本地 |
| 缓存磁盘满 | Cache | 大小检查 | LRU 淘汰 → ENOSPC |
| .buf 文件损坏 | WriteBuffer | JSON 解析 | 跳过，记录警告 |
| 路径不存在 | DirTree | resolve=None | ENOENT |
| 后台加载失败 | DirTree | exception | warn 日志；ensure_loaded 独立路径 |
| 空容器 | Lifecycle | list 返回空 | 回退 "applicationData" |
| 文件夹名称不存在 | Lifecycle | 遍历无匹配 | MountError + 可用列表 |

## 6. 测试覆盖

共 **165** 个测试，覆盖所有模块:

| 测试文件 | 覆盖内容 | 关键场景 |
|---------|---------|---------|
| test_config | 配置验证、环境变量、JSON、冻结 | 边界值、缺失字段 |
| test_token | 双模式、缓存重读、401 | 过期/空文件 |
| test_client | queryParam、分页、multipart、401 | API 交互正确性 |
| test_dirtree | 路径解析、增删改、hidden file | parentField 兼容 |
| test_cache | LRU 淘汰、持久化恢复、并发 | 超限淘汰正确性 |
| test_writebuf | enqueue、drain、flush_all、重试 | 单写者冲突 |
| test_fuse | 全部 POSIX 操作 | ensure_loaded 前置 |
| test_lazy_load | load_dir、ensure_loaded、bg_load、并发 | 18 个专项测试 |
| test_lifecycle | 根发现、文件夹解析、状态报告 | 三种 cloud_folder 模式 |
| test_perf | 基准测试 | 操作延迟 |
| test_real_perf | 真实 API（mark.realapi） | 端到端验证 |

**并发安全测试**: 同一目录并发 load_dir、ensure_loaded 与 background_full_load 并发、Condition wait/notify 正确性。

单元测试使用 mock DriveKitClient，不调用真实 API。
