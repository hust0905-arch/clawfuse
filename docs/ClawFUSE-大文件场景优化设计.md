# ClawFUSE 大文件场景优化设计

> 版本: 1.0 | 日期: 2026-04-25
> 基于 ClawFUSE v0.1.0（当前已发布版本）

## 1. 问题背景

### 1.1 原始方案

OpenClaw 容器当前采用**压缩包**方式同步用户数据：将用户整个 `claw` 目录打包为一个压缩文件上传到 Drive Kit。容器启动时下载并解压。

### 1.2 原始方案的痛点

| 痛点 | 说明 |
|------|------|
| 启动极慢 | 20GB 压缩包需完整下载 + 解压，网络 10MB/s 时至少 2000 秒（33 分钟） |
| 全量传输 | 用户可能只访问其中 1% 的文件，但仍需全量下载 |
| 不支持增量 | 每次保存都是全量覆盖，频繁操作导致压缩包越来越大 |

### 1.3 FUSE 方案的预期

用 ClawFUSE 替代压缩包方案，实现：
- 按需加载：只下载用户实际访问的文件
- 秒级启动：挂载无需等待全量数据
- 透明读写：Agent 无感知，和本地文件系统一样使用

### 1.4 当前 ClawFUSE 在此场景下的核心缺陷

当前 ClawFUSE 的启动流程：

```
pre_start()
  ├── ensure_dirs()              < 10ms
  ├── TokenManager 初始化        < 10ms
  ├── DriveKitClient 创建        < 10ms
  ├── resolve_root_folder()      ~500ms（1 次 API）
  ├── DirTree.refresh()          ← 瓶颈！全量 BFS 遍历
  │     └── BFS 串行遍历每个文件夹
  │           ├── 每页 ~500ms（API 延迟）
  │           ├── 每个文件夹可能多页
  │           └── 文件夹嵌套越多越慢
  └── Cache/WriteBuffer 初始化   < 10ms

→ FUSE 挂载（在 refresh 完成之后）
```

**关键问题：`DirTree.refresh()` 是阻塞式全量 BFS，在 FUSE 挂载之前必须完成。**

对于 20GB、数万文件的场景，实测估算：

| 文件数量 | API 页请求数 | 预估 BFS 耗时 | 说明 |
|----------|-------------|--------------|------|
| 1,000 | ~5 页 | 3-5s | 可接受 |
| 5,000 | ~25 页 | 15-25s | 有体感延迟 |
| 10,000 | ~50 页 | 30-60s | 用户不可接受 |
| 50,000 | ~250 页 | 3-5min | 比压缩包方案更慢 |
| 100,000 | ~500 页 | 5-10min | 完全不可用 |

**结论：对于大文件场景，当前 ClawFUSE 的 BFS 启动策略反而比原始压缩包方案更慢，必须优化。**

> **P0 和 P1 必须同时实施，不可单独做。** P0 单独实施（跳过 BFS、直接挂载）会导致 DirTree 为空，用户访问任何路径都会报 `ENOENT`（文件不存在）。P0 和 P1 合在一起才是完整的「延迟挂载 + 按需加载」方案。

## 2. 问题分析

### 2.1 当前设计假设 vs 实际场景

| 维度 | 当前设计假设 | 实际场景 |
|------|------------|---------|
| 文件数量 | 数百到数千 | 数千到数万 |
| 数据总量 | MB 到低 GB | 高达 20GB |
| 容器生命周期 | 长期运行，重启少 | 临时容器，频繁冷启动 |
| 缓存持久性 | 缓存跨容器存活 | 容器销毁后缓存清空 |
| 访问模式 | 全量访问 | 只访问少量文件 |

### 2.2 六个核心问题

#### 问题 1：BFS 全量加载阻塞挂载

**位置：** `dirtree.py:57-64` `refresh()` 方法、`lifecycle.py:94`

**现状：** `pre_start()` 调用 `self._dirtree.refresh()`，该方法通过 `client.list_all_files()` 做 BFS 遍历，获取所有文件元数据后才返回。FUSE 挂载在 `pre_start()` 之后才执行。

**影响：** 数万文件时启动时间 3-10 分钟，挂载前用户完全无法使用。

**根因：** 设计假设「元数据全量预加载是必要的」，但实际上大部分文件不会被访问。

#### 问题 2：DirTree 不支持按目录加载（P0 不能单独做的原因）

> **如果单独跳过 `refresh()` 而不实现按需加载，用户体验如下：**
>
> ```
> 用户执行：cat /data/report.csv
>
> FUSE 调用 getattr("/data/report.csv")
>   → dirtree.resolve("/data/report.csv")
>   → _path_map 为空 → 返回 None
>   → fuse.py 报 errno.ENOENT "No such file or directory"
>
> 但文件实际在云端存在！只是 DirTree 还没加载它的元数据。
> ```
>
> 同理，`ls /data` 会返回空目录（`list_dir` 查 `_children_map` 为空）。
> 所以 P0（跳过 BFS）必须配合 P1（按需加载）一起实施。

**位置：** `dirtree.py` 整体设计

**现状：** DirTree 的数据结构是 `_path_map: dict[str, FileMeta]`（全量路径映射），`resolve()` 和 `list_dir()` 都假设所有数据已在内存中。没有「只加载某个目录」的能力。

**影响：** 无法改为按需加载，必须重构 DirTree。

#### 问题 3：FUSE getattr 在路径不存在时直接报错

**位置：** `fuse.py:55-66`

**现状：** `getattr()` 调用 `self._dirtree.resolve(path)`，如果返回 None 则直接抛 `ENOENT`。没有「尝试加载该路径」的机制。

**影响：** 即使 DirTree 支持按需加载，FUSE 层也不会触发加载。

#### 问题 4：首次文件读取有 0.8-1.5s 延迟

**位置：** `fuse.py:103-113`

**现状：** 缓存未命中时，`read()` 同步调用 `client.download_file()` 下载整个文件内容。Drive Kit API 固定延迟 ~800ms。

**影响：** 用户打开每个新文件都有明显卡顿。如果 Agent 启动时连续读取多个文件（配置、索引等），会串行等待。

**根因：** 没有预取机制。知道用户会访问哪些文件时，没有提前下载。

#### 问题 5：API 调用全串行

**位置：** `client.py:234-269` `list_all_files()`

**现状：** BFS 遍历时，文件夹一个接一个处理。drain 上传也是单线程串行。

**影响：** 网络带宽没有充分利用。Drive Kit API 的网络等待时间（~500ms/call）可以并行重叠。

#### 问题 6：容器重启后缓存全部丢失

**位置：** `cache.py`

**现状：** 缓存存储在容器本地文件系统（`/tmp/clawfuse-cache`），容器销毁后丢失。

**影响：** 每次容器启动都是全冷启动，没有任何「热」数据。

> ## Drive Kit API 并发能力实测（2026-04-25）
>
> 使用真实 Drive Kit API 测试，从 1 到 32 并发发送 list_files / get_file_metadata 请求，持续 10 秒。
>
> **结论：Drive Kit API 无并发限流（0 次 429/503），但 QPS 天花板在 ~5.8。**
>
> | 并发数 | QPS | 平均延迟 | 限流次数 | 说明 |
> |--------|-----|---------|---------|------|
> | 1（串行） | 1.45 | 689ms | 0 | 基线 |
> | 2 | 1.93 | 518ms | 0 | |
> | 4 | 3.65 | 274ms | 0 | |
> | **8** | **4.63** | **216ms** | **0** | **最优性价比** |
> | 16 | 4.74 | 211ms | 0 | 几乎不再提升 |
> | 32 | 5.71 | 175ms | 0 | 到顶 |
>
> **对优化的影响：**
> - 后台 BFS 并发度设 8 即可，再高没有收益
> - 不需要退避/限流策略（API 不限流）
> - 并发 8 时用户请求平均延迟 216ms，可接受
> - QPS 5.8 → 1000 个目录 BFS 约 3 分钟（后台不阻塞用户）
>
> 详见 `docs/concurrency_test_results.json`

## 3. 解决方案

### 3.1 核心思路

**三层异步加载模型：**

```
优化前：
  pre_start() → BFS全量(3-10min) → FUSE mount → 用户使用
                  ↑ 阻塞

优化后：
  pre_start() → FUSE mount(< 1s) → 用户立即使用
                   │                  │
                   │                  ├── 用户请求来了 → 检查元数据是否已加载
                   │                  │     ├── 已加载 → 直接响应（< 0.02ms）
                   │                  │     └── 未加载 → 优先加载该目录 → 再响应（~500ms）
                   │                  │
                   │                  └── 用户读取文件 → 文件内容按需下载 + 缓存
                   │
                   └── 后台线程：全量 BFS 加载元数据 ASAP
                         ├── 挂载后立即启动，尽最大可能快速完成
                         ├── 多线程并行加载，充分利用 API 并发能力
                         └── 已被用户请求提前加载的目录自动跳过
```

**设计原则：**

1. **元数据全量加载是目标，不是按需。** 挂载后立即启动后台线程全力 BFS，目标是尽快把所有文件元数据加载到内存
2. **启动不阻塞。** FUSE 挂载不等待元数据加载完成，用户秒级可用
3. **用户请求优先。** 后台加载到一半时用户要访问某个还没加载到的目录 → 立刻优先加载该目录，用户请求优先于后台队列
4. **文件内容按需加载。** 文件内容（可能 20GB）始终只在用户实际打开时才下载，不做全量预取（小文件可选预取）

### 3.2 阶段 1：延迟挂载 + 后台全量加载 + 用户请求优先

> P0 和 P1 合并为一个阶段，因为它们必须一起实施。单独跳过 BFS 会导致 DirTree 为空，用户访问任何路径都会报 ENOENT。

**目标：** 启动时间从分钟级降到亚秒级，同时元数据全量加载尽快完成。

**改动范围：** `lifecycle.py`、`dirtree.py`、`fuse.py`

**方案：**

#### 3.2.1 lifecycle.py：跳过 refresh，挂载后启动后台全量加载

```python
# 当前（阻塞式）
self._dirtree.refresh()           # 阻塞 3-10 分钟

# 改为（非阻塞）
self._dirtree.set_root_folder(root_folder)  # 仅设置根目录 ID

# 挂载后立即启动后台全量加载（不是可选的，是必须的）
threading.Thread(
    target=self._dirtree.background_full_load,
    args=(self._client,),
    daemon=True,
).start()
```

后台线程 `background_full_load` 的职责：**以最快速度把所有目录元数据加载到内存。** 不是"有空再加载"，而是"全力以赴尽快加载完"。

#### 3.2.2 dirtree.py：新增目录级加载能力

**数据结构变更：**

```python
class DirTree:
    # 现有字段保留
    _path_map: dict[str, FileMeta]
    _id_map: dict[str, str]
    _children_map: dict[str, list[FileMeta]]

    # 新增
    _loaded_dirs: set[str]        # 已从 API 加载过的目录 ID
    _loading: set[str]            # 正在加载中的目录 ID（防止并发重复加载）
    _loading_lock: threading.Lock # 保护 _loading 集合
```

**新增方法：**

```python
def load_dir(self, dir_id: str) -> None:
    """加载单个目录的直接子项元数据。

    线程安全。后台线程和用户请求都调用此方法。
    通过 _loaded_dirs + _loading + Lock 实现并发安全。
    """

def ensure_loaded(self, dir_path: str) -> None:
    """确保 dir_path 及其所有祖先目录都已加载。

    被 FUSE getattr/readdir 调用。从根逐级调用 load_dir，
    已加载的目录跳过，只加载缺失的中间路径。
    """

def background_full_load(self, client: DriveKitClient) -> None:
    """后台全量加载：BFS 从根目录开始，以最快速度加载所有目录。

    被 lifecycle.py 启动的 daemon 线程调用。
    遇到已加载的目录（被用户请求提前加载的）自动跳过。
    """
```

#### 3.2.3 并发实现：`load_dir` 是唯一的加载入口

**没有优先队列，没有调度器。** 后台线程和用户请求都调用同一个 `load_dir()`，通过 `_loaded_dirs` 和 `_loading` 集合协调：

```python
def load_dir(self, dir_id: str) -> None:
    """加载单个目录。后台线程和用户请求共享此方法。"""
    # 1. 快速检查：已加载过？直接返回
    if dir_id in self._loaded_dirs:
        return

    # 2. 加锁：防止两个线程同时加载同一个目录
    with self._loading_lock:
        if dir_id in self._loaded_dirs:  # double-check
            return
        if dir_id in self._loading:      # 另一个线程正在加载
            # 等它加载完
            while dir_id in self._loading:
                self._loading_lock.release()
                time.sleep(0.01)
                self._loading_lock.acquire()
            return
        self._loading.add(dir_id)        # 标记"正在加载"

    try:
        # 3. 调 API 获取该目录的子项（无论谁调的，都走同样的逻辑）
        result = self._client.list_files(parent_folder=dir_id)
        files = result.get("files", [])

        # 4. 处理分页
        cursor = result.get("nextCursor")
        while cursor:
            page = self._client.list_files(parent_folder=dir_id, cursor=cursor)
            files.extend(page.get("files", []))
            cursor = page.get("nextCursor")

        # 5. 将子项写入内存索引
        with self._loading_lock:
            for item in files:
                # ... 构建 FileMeta，写入 _path_map / _children_map / _id_map ...
                pass
            self._loaded_dirs.add(dir_id)   # 标记"已加载"
    finally:
        with self._loading_lock:
            self._loading.discard(dir_id)   # 清除"正在加载"标记
```

#### 3.2.4 "插队"具体是怎么发生的

没有显式的优先级机制。"优先"是自然发生的，因为用户请求是同步的，后台是异步的：

```
时间线：

t=0.0s  后台线程: load_dir(root)                          ← API 调用中
t=0.5s  后台线程: root 加载完，发现子目录 /data, /docs
t=0.5s  后台线程: load_dir(/docs)                          ← API 调用中
t=1.0s  用户请求: ls /data/reports                         ← 用户操作！
t=1.0s  ┌── FUSE readdir 线程 ──────────────────────────┐
        │  ensure_loaded("/data/reports")                 │
        │    ├── "/data" 在 _loaded_dirs 里吗？ → 否      │
        │    │   后台还在加载 /docs，/data 还没轮到        │
        │    │   → 用户请求直接调 load_dir(/data)          │
        │    │   → API 调用（500ms）                       │
        │    ├── "/data/reports" 在 _loaded_dirs 吗？→ 否  │
        │    │   → 用户请求直接调 load_dir(/reports)       │
        │    │   → API 调用（500ms）                       │
        │    └── 返回 /data/reports 的子项给用户            │
        └────────────────────────────────────────────────┘
t=2.0s  用户看到 /data/reports 的文件列表

t=1.0s  后台线程: /docs 加载完
t=1.0s  后台线程: load_dir(/data)                          ← 但 _loading 中有 /data
        │   → 等待...（用户请求正在加载 /data）
        │   → 用户请求加载完 /data，从 _loading 移除
        │   → 后台线程发现 /data 已在 _loaded_dirs → 跳过
t=1.5s  后台线程: 继续加载 /data 的其他子目录...
```

**关键点：**
- 用户请求在 FUSE 的线程中**同步执行**，不等后台队列
- 后台线程到同一目录时，发现 `_loaded_dirs` 里已经有了，直接跳过
- 如果两者同时想加载同一个目录，`_loading` 锁保证只有一个去调 API，另一个等待
- 用户不会感知到后台线程的存在，该等 500ms 就等 500ms

#### 3.2.5 后台全量加载的实现

```python
def background_full_load(self, client: DriveKitClient) -> None:
    """后台 BFS 全量加载。挂载后立即调用，全力跑完。

    并发度 8：实测 Drive Kit API 并发 8 时 QPS ~4.6，再高无收益。
    """
    queue = [self._root_folder]  # BFS 队列

    while queue:
        dir_id = queue.pop(0)

        # load_dir 内部检查 _loaded_dirs，已加载的瞬间返回
        self.load_dir(dir_id)

        # 收集该目录下的子文件夹，加入 BFS 队列
        children = self._children_map.get(dir_id, [])
        for child in children:
            if child.is_dir:
                queue.append(child.id)

    logger.info("Background full load complete: %d directories", len(self._loaded_dirs))
```

> **注意：** 后台线程和用户请求调用的是**完全相同的 `load_dir()`**。没有两套代码。区别只是调用者不同：后台线程按 BFS 顺序调用，用户请求按 `ensure_loaded` 逐级调用。

#### 3.2.6 `ensure_loaded`：用户请求触发的逐级加载

当用户访问深层路径时，从根开始逐级检查和加载，已加载的目录跳过：

```
用户访问 /data/reports/2026/summary.csv

ensure_loaded("/data/reports/2026")
  │
  ├── 1. 检查 "/" → 后台已加载 ✓ → 直接拿 /data 的 dir_id（0ms）
  ├── 2. 检查 "/data" → 后台还没加载到这里 → load_dir(data_id)    ~500ms
  ├── 3. 检查 "/data/reports" → 未加载 → load_dir(reports_id)     ~500ms
  └── 4. 检查 "/data/reports/2026" → 未加载 → load_dir(dir_26_id) ~500ms

总耗时：3 次 API ≈ 1.5s（第 1 级跳过，只加载 2-4 级）
后台线程稍后到 /data 时发现已加载，跳过
```

```python
def ensure_loaded(self, dir_path: str) -> None:
    """确保 dir_path 及其所有祖先目录都已加载。"""
    parts = PurePosixPath(dir_path).parts
    current_path = "/"
    current_id = self._root_folder

    for part in parts[1:]:
        child_path = current_path.rstrip("/") + "/" + part
        # load_dir 内部检查 _loaded_dirs，已加载的瞬间返回
        self.load_dir(current_id)

        child_meta = self._path_map.get(child_path)
        if child_meta is None:
            break  # 路径不存在，getattr 会报 ENOENT
        current_path = child_path
        current_id = child_meta.id
```

#### 3.2.7 FUSE 层改动（`fuse.py`）

```python
# 当前
def getattr(self, path, fh=None):
    meta = self._dirtree.resolve(path)
    if meta is None:
        self._raise(errno.ENOENT, path)   # 直接报错

# 改为
def getattr(self, path, fh=None):
    meta = self._dirtree.resolve(path)
    if meta is None:
        # 尝试从根目录逐级加载，直到找到目标或确认不存在
        parent = str(PurePosixPath(path).parent)
        self._dirtree.ensure_loaded(parent)   # ← 逐级加载
        meta = self._dirtree.resolve(path)    # 再查一次
    if meta is None:
        self._raise(errno.ENOENT, path)       # 确实不存在才报错
```

**readdir 同样需要改动（`fuse.py`）：**

```python
# 当前
def readdir(self, path, fh):
    entries = self._dirtree.list_dir(path)
    return [".", ".."] + entries

# 改为
def readdir(self, path, fh):
    # 确保该目录已加载
    self._dirtree.ensure_loaded(path)
    entries = self._dirtree.list_dir(path)
    return [".", ".."] + entries
```

**性能预期：**

| 操作 | 首次延迟 | 后续延迟 | 说明 |
|------|---------|---------|------|
| `ls /` | ~500ms（1 次 API） | < 0.02ms | 加载根目录 |
| `ls /data` | ~500ms（1 次 API） | < 0.02ms | 加载 /data |
| `ls /data/reports/2026` | ~1.5s（3 次 API，逐级加载） | < 0.02ms | 深层路径首次较慢 |
| `cat /data/report.csv` | ~1s（目录 + 文件下载） | < 0.2ms | 目录已加载则只需下载文件 |

> **深层路径首次慢的问题很快会缓解：** 后台线程全力 BFS，2-3 秒后大部分常用目录已在内存中。用户极少遇到逐级加载。

### 3.3 阶段 2：小文件预取

**目标：** 用户打开文件时，如果已在缓存中则 < 1ms 响应。

**改动范围：** `dirtree.py`（加载目录时标记小文件）、`cache.py`（新增预取接口）

**方案：**

在 `load_dir()` 返回目录子项后，后台预下载该目录下所有小文件（< 阈值，默认 100KB）。

```python
def load_dir(self, dir_id: str) -> list[FileMeta]:
    # ... 从 API 加载目录元数据 ...

    # 触发小文件预取（非阻塞）
    small_files = [m for m in children if not m.is_dir and m.size < self._prefetch_threshold]
    if small_files:
        self._prefetch_executor.submit(self._prefetch_small_files, small_files)

    return children

def _prefetch_small_files(self, files: list[FileMeta]) -> None:
    """后台下载小文件到缓存。"""
    for f in files:
        if self._cache.has(f.id):
            continue  # 已缓存，跳过
        content = self._client.download_file(f.id)
        self._cache.put(f.id, f.path, content, f.sha256)
```

**配置项：**

```json
{
  "prefetch_enabled": true,
  "prefetch_max_kb": 100,
  "prefetch_concurrency": 4
}
```

**预取策略：**

| 文件大小 | 预取行为 | 原因 |
|---------|---------|------|
| < 10KB | 立即预取 | 配置文件、索引文件，几乎一定会被读取 |
| 10KB - 100KB | 批量预取 | 常用文件，下载快（~800ms） |
| 100KB - 1MB | 不预取 | 预取代价大，按需下载 |
| > 1MB | 不预取 | 大文件只在用户明确打开时下载 |

**效果预估：**

对于典型 claw 目录，小文件（配置、索引）占比约 80% 的文件数量，但仅占 5% 的数据量。预取后用户打开这些文件时 < 1ms。

### 3.4 阶段 3：并行 API 调用

**目标：** 通过并发请求提升整体吞吐。

**改动范围：** `client.py`、`dirtree.py`、`writebuf.py`

**方案：**

使用 `concurrent.futures.ThreadPoolExecutor` 实现并发：

#### 3.6.1 并行 BFS 预加载

```python
def background_refresh_parallel(self, max_workers: int = 4) -> None:
    """并行 BFS 预加载。"""
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        folders_to_process = [self._root_folder]

        while folders_to_process:
            # 提交当前一批文件夹的加载任务
            futures = {
                pool.submit(self.load_dir, fid): fid
                for fid in folders_to_process
            }
            folders_to_process = []

            for future in as_completed(futures):
                children = future.result()
                # 收集子文件夹，作为下一批任务
                for child in children:
                    if child.is_dir:
                        folders_to_process.append(child.id)
```

#### 3.6.2 并行文件预取

```python
def _prefetch_small_files(self, files: list[FileMeta]) -> None:
    """并行预取小文件。"""
    with ThreadPoolExecutor(max_workers=self._prefetch_concurrency) as pool:
        futures = [pool.submit(self._prefetch_one, f) for f in files]
        wait(futures, timeout=10)  # 最多等 10 秒
```

#### 3.6.3 并行 drain 上传

```python
# writebuf.py _drain_loop 改为：
def _drain_one_batch(self) -> None:
    pending = self._get_pending_batch(max_batch=4)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(self._upload_one, pw) for pw in pending]
        # ... 收集结果 ...
```

**并行度配置：**

```json
{
  "bfs_concurrency": 4,
  "prefetch_concurrency": 4,
  "drain_concurrency": 2
}
```

### 3.5 阶段 4：元数据快照（可选，长期优化）

**目标：** 进一步加速二次启动。

**方案：** 容器销毁前（`pre_destroy` 阶段），将当前 DirTree 序列化为 JSON 文件上传到 Drive Kit。下次容器启动时，先下载这个快照文件（< 1MB），瞬间恢复目录树。

```
首次启动：
  FUSE mount → 按需加载 → 后台 BFS 全量加载

二次启动：
  FUSE mount → 下载快照文件(~500ms) → DirTree 从快照恢复(< 100ms)
  → 后台验证快照是否过期 → 按需更新
```

**快照文件格式：**

```json
{
  "version": 1,
  "root_folder": "applicationData",
  "created_at": "2026-04-25T10:00:00Z",
  "file_count": 12345,
  "items": [
    {"id": "xxx", "name": "data", "is_dir": true, "parent_id": "applicationData", ...},
    {"id": "yyy", "name": "report.csv", "is_dir": false, "size": 1024, "sha256": "...", ...}
  ]
}
```

**注意事项：**
- 快照可能过期（其他设备修改了云文件），需要校验机制
- 首次启动没有快照，回退到按需加载
- 快照文件存储在 Drive Kit 的隐藏目录中

## 4. 改动影响评估

### 4.1 改动量估算

| 阶段 | 改动项 | 涉及文件 | 代码行数估算 | 测试用例 |
|------|--------|---------|-------------|---------|
| 阶段 1 | 延迟挂载 + 后台全量加载 + 用户请求优先 | lifecycle.py, dirtree.py, fuse.py | ~230 行 | 15-20 个 |
| 阶段 2 | 小文件预取 | dirtree.py, cache.py | ~80 行 | 5-8 个 |
| 阶段 3 | 并行 API 调用 | client.py, dirtree.py, writebuf.py | ~100 行 | 5-8 个 |
| 阶段 4 | 元数据快照（可选） | dirtree.py, lifecycle.py, client.py | ~200 行 | 8-12 个 |

### 4.2 向后兼容性

| 阶段 | 兼容性 | 说明 |
|------|--------|------|
| 阶段 1 | ✅ 完全兼容 | CLI 参数不变，启动更快，行为不变 |
| 阶段 2 | ✅ 完全兼容 | 新增配置项，有默认值 |
| 阶段 3 | ✅ 完全兼容 | 内部实现改变，接口不变 |
| 阶段 4 | ⚠️ 需新增文件 | 在 Drive Kit 中创建隐藏目录存储快照 |

### 4.3 风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 后台全量加载期间用户访问未加载目录 | 用户感知首次 ls 慢（~500ms） | 后台全力加载，2-3s 后大部分目录已缓存 |
| 并发 API 调用可能触发 Drive Kit 限流 | API 请求失败 | 并发度可配置，默认保守值 4 |
| 后台加载和用户请求并发加载同一目录 | 浪费一次 API 调用 | 使用 `_loading` 集合 + Lock 防止重复 |
| 小文件预取消耗带宽 | 影响用户正常下载 | 预取阈值可配置，仅预取 < 100KB |
| 快照过期导致目录树不准确 | 用户看到旧文件 | 后台校验 + TTL 机制（阶段 4） |

## 5. 实施计划

### 5.1 分阶段实施

```
阶段 1（核心）— 延迟挂载 + 后台全量加载 + 用户请求优先
  ├── 重构 DirTree：新增 load_dir()、ensure_loaded()、_loaded_dirs
  ├── 新增后台全量加载线程：background_full_load()
  ├── 改造 fuse.py：getattr/readdir 调用 ensure_loaded()
  ├── 改造 lifecycle.py：跳过 refresh()，启动后台线程，直接挂载
  └── 新增测试：目录加载、用户请求优先、逐级加载、后台加载不阻塞

阶段 2 — 小文件预取
  ├── load_dir 后触发小文件预取
  ├── 新增配置项
  └── 新增测试：预取命中

阶段 3 — 并行 API 提升吞吐
  ├── 并行 BFS（同级子目录并发加载）
  ├── 并行预取
  ├── 并行 drain 上传
  └── 新增测试：并发正确性

阶段 4（可选）— 元数据快照
  ├── 快照序列化/反序列化
  ├── 快照上传/下载
  ├── 过期校验
  └── 新增测试：快照恢复
```

### 5.2 验证标准

| 指标 | 当前 | 阶段 1 后 | 阶段 2 后 | 最终目标 |
|------|------|----------|----------|---------|
| 挂载启动时间（1000 文件） | ~5s | < 1s | < 1s | < 1s |
| 挂载启动时间（50000 文件） | ~3min | < 1s | < 1s | < 1s |
| 元数据全部加载完成 | ~5s（阻塞） | ~30s（后台，不阻塞） | ~15s（并行） | ~10s |
| 首次 ls（后台未覆盖的目录） | < 0.02ms | ~500ms | ~500ms | ~500ms |
| 首次 ls（后台已覆盖的目录） | < 0.02ms | < 0.02ms | < 0.02ms | < 0.02ms |
| 首次打开小文件 | ~800ms | ~1300ms | < 5ms（预取） | < 5ms |
| 缓存命中读文件 | ~0.2ms | ~0.2ms | ~0.2ms | ~0.2ms |
| 二次启动（有快照） | N/A | N/A | N/A | < 2s |

### 5.3 测试策略

| 测试类型 | 内容 |
|---------|------|
| 单元测试 | DirTree.load_dir()、ensure_loaded() 逐级加载、_loading 并发控制 |
| 集成测试 | FUSE getattr → ensure_loaded → load_dir → mock API 返回 → 缓存命中 |
| 并发测试 | 后台全量加载 + 用户请求同时触发同一目录 → 不重复加载 |
| 性能测试 | 模拟 10000/50000 文件目录树，测量启动时间、首次访问延迟、后台加载完成时间 |
| 真实 API 测试 | 使用真实 Drive Kit 验证按需加载和预取效果 |
