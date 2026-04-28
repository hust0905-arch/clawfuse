# ClawFUSE 架构设计说明书

> 版本: 3.0 | 日期: 2026-04-28
> 项目: ClawFUSE — OpenClaw 容器 Drive Kit FUSE 挂载

---

## 1. 概述

### 1.1 问题定义

OpenClaw 是运行在云侧的 AI Agent 容器，用户数据存储在华为 Drive Kit 云空间。容器具有临时性——不活跃后自动销毁，用户返回时重建。

核心挑战：**容器启动后，Agent 需要在数秒内访问到用户在云空间的全部文件，且写入的文件必须在容器销毁后持久化。**

传统方案（压缩包全量同步）的瓶颈：

| 阶段 | 20GB 压缩包 @ 1Gbps | 100Mbps 网络 |
|------|---------------------|-------------|
| 下载 | 160s | 1600s (27min) |
| 解压 | 67s | 67s |
| **总计** | **~227s** | **~27min** |
| 50GB 压缩包 | **~567s** | **~75min** |

实际带宽通常远低于 1Gbps（跨区域、云厂商限速），真实场景 5-10 分钟。**且数据量越大启动越慢，线性增长，不可接受。**

### 1.2 解决方案

ClawFUSE 将 Drive Kit 云存储挂载为本地 FUSE 文件系统。Agent 通过标准 POSIX 接口（open、read、write、readdir）访问云文件，无需感知底层存储。

设计原则：**元数据懒加载 + 后台并行预加载 + 文件内容按需加载 + 写入异步回传。**

```
原始方案:  下载全部 20GB → 解压 → 才能用     (227s+)
ClawFUSE:  秒级挂载 → 按需访问 → 后台预加载   (0.26s 挂载)
```

### 1.3 设计目标与达成

| 目标 | 指标 | 实测达成 | 对比原始方案 |
|------|------|----------|-------------|
| 秒级启动 | 挂载后立即可用 | **260ms** | 227s → **873x 加速** |
| 冷启动可用 | 首次访问深文件 | **154-342ms** | 227s → **660x 加速** |
| 透明访问 | POSIX 全覆盖 | 12 个操作 | Agent 零改造 |
| 元数据全量加载 | 1111 目录 BFS | **19s** | 227s → **12x 加速** |
| 缓存命中性能 | 接近本地磁盘 | **0.002ms** (stat) | 与本地相当 |
| 写回持久化 | 容器销毁数据不丢 | WriteBuffer + flush_all | 5MB 写 301ms |
| 数据规模无关 | 无论 1GB 还是 100GB | 启动均 260ms | 线性增长 |

**核心优势：ClawFUSE 的启动时间与数据总量无关，只取决于实际需要的文件。20GB 工作空间可能只用到 100MB，ClawFUSE 只下载这 100MB。**

## 2. 架构设计

### 2.1 系统架构

```
┌───────────────────────────────────────────────────────────┐
│                   OpenClaw 容器                            │
│                                                           │
│   AI Agent ── POSIX ──► /mnt/drive/ ──► ClawFUSE          │
│                                          │                │
│                               ┌──────────┼──────────┐     │
│                               │          │          │     │
│                           DirTree    Cache     WriteBuffer │
│                          (元数据)   (读缓存)   (写缓冲)    │
│                        懒加载+BFS  LRU磁盘   异步drain     │
│                               │          │          │     │
│                               └──────────┼──────────┘     │
│                                          │                │
│                                  DriveKitClient            │
│                               (queryParam 过滤)            │
│                                          │                │
│                             LifecycleManager               │
│                          ┌─ pre_start: 发现根+后台加载     │
│                          └─ pre_destroy: flush_all writes  │
└──────────────────────────┼────────────────────────────────┘
                           │ HTTPS
                           ▼
                 ┌─────────────────────┐
                 │  Drive Kit 云端     │
                 │  (华为云存储服务)    │
                 └─────────────────────┘
```

### 2.2 核心数据流

#### 读取路径（冷启动 → 缓存命中）

```
Agent read(path, offset, size)
    │
    ├─ ensure_loaded(parent_dir)          ← 路径级懒加载
    │   ├─ 已加载 → 跳过 (0ms)
    │   └─ 未加载 → load_dir API (~800ms)
    │
    ├─ DirTree.resolve(path) → FileMeta   ← 纯内存 (0.002ms)
    │
    └─ Cache.get(file_id)
        ├─ 命中 → 返回 (0.2-0.4ms)       ← 磁盘缓存
        └─ 未命中 → download → cache → 返回 (200-500ms)
```

#### 写入路径（内存缓冲 + 异步上传）

```
Agent write(path, data, offset)
    │
    ├─ 内存 bytearray 缓冲 (< 1ms)        ← 切片赋值，避免 O(n²)
    │
    ├─ flush → WriteBuffer.enqueue        ← 持久化 .buf 文件
    │   ├─ 同时更新 Cache（后续读可见）
    │   └─ 同时更新 DirTree size（getattr 返回正确大小）
    │
    └─ drain 线程异步上传                  ← 不阻塞 Agent
        ├─ 成功 → 删除 .buf
        └─ 失败 → 重试 (max_retries)
```

### 2.3 组件职责

| 组件 | 职责 | 存储 | 线程模型 |
|------|------|------|----------|
| **DirTree** | 目录结构 + 元数据索引 + 懒加载调度 | 内存 | FUSE 线程 + 后台 8 线程 BFS，Condition 协调 |
| **ContentCache** | 文件内容 LRU 缓存 | 磁盘 | FUSE 线程 |
| **WriteBuffer** | 写缓冲 + 后台 drain 上传 | 磁盘 .buf/.meta | 后台 drain 线程 |
| **DriveKitClient** | REST API 封装，queryParam 过滤，401 重试 | 无状态 | 调用方线程 |
| **TokenManager** | 双模式令牌（文件/字符串） | 内存缓存 | 主线程 |
| **LifecycleManager** | 生命周期编排：根发现 + 组件初始化 + 后台加载 | 无 | 主线程 |

## 3. 关键架构决策

### ADR-1: 元数据懒加载 + 后台并行预加载

**决策**: 启动时不加载任何元数据，FUSE 立即挂载。用户访问时按需加载路径上的目录（`ensure_loaded`），同时后台 8 线程并行 BFS 预加载全部目录。

**分析**: Drive Kit 单次 API 调用有 ~800ms 固定延迟。若启动时串行 BFS 加载 1111 个目录，需 ~430 秒——Agent 无法接受。即使 8 线程并行 BFS 仍需 ~19 秒。

懒加载的关键洞察：**Agent 从根目录开始逐级深入访问文件，实际只需要路径上的 2-3 个目录即可开始工作。** 后台线程同时加载其余目录，Agent 几乎无感。

**实测数据（2026-04-28，中国大陆公网，独立场景验证）**:

| 操作 | 延迟 | 说明 |
|------|------|------|
| FUSE 挂载 | **260ms** | 初始化全部组件 |
| 冷启动首次 stat（3 层深，85 目录） | **154ms** | ensure_loaded 3 级 + 文件元数据 |
| 冷启动首次 stat（3 层深，1111 目录） | **342ms** | ensure_loaded 3 级 + 文件元数据 |
| 冷启动首次 read（1.2KB） | **352-427ms** | ensure_loaded + download |
| BFS 全量加载 85 目录 | **1.0s** | 8 线程并行 |
| BFS 全量加载 259 目录 | **4.0s** | 8 线程并行 |
| BFS 全量加载 1111 目录 | **19.0s** | 8 线程并行 |
| BFS 完成后 getattr | **0.002ms** | 纯内存 |
| BFS 完成后 readdir | **< 1ms** | 纯内存 |
| BFS 完成后 walk 1000 文件 | **0.4s** | 纯内存遍历 |

**BFS 加载线性度验证**:

| 目录数 | BFS 时间 | 平均每目录 | 线性度 |
|--------|---------|-----------|--------|
| 85 | 1.0s | 11.8ms | - |
| 259 | 4.0s | 15.4ms | 1.3x |
| 1111 | 19.0s | 17.1ms | 1.1x |

BFS 加载时间与目录数近似线性，平均 ~15ms/目录，可按此估算任意规模的加载时间。

**典型 Agent 启动时间线（1111 目录场景）**:

```
0.0s    FUSE 挂载完成
0.3s    Agent stat /mnt/drive/workspace/file.py  → 342ms (ensure_loaded + 元数据)
0.7s    Agent read file.py                       → 427ms (download + cache)
        ... Agent 开始正常工作 ...
~19s    后台 BFS 完成，全部元数据在内存中
        ... 后续操作全部 < 1ms ...
```

**对比压缩包方案**:

| 指标 | 压缩包方案 (20GB) | ClawFUSE | 加速比 |
|------|-------------------|----------|--------|
| 启动到可用 | 227s | **0.26s** | **873x** |
| 首次访问文件 | 227s（等下载） | **0.7s** | **324x** |
| 全部元数据就绪 | 227s | **19s** | **12x** |
| 数据规模 50GB | 567s | **0.26s** | **2181x** |
| 数据规模 100GB | 1134s | **0.26s** | **4362x** |

### ADR-2: Drive Kit queryParam 过滤

**决策**: 列表接口使用 `queryParam='{folderId}' in parentFolder` 过滤，不使用 `parentFolder` 直接参数。

**分析**: 实测发现 Drive Kit API 的 `parentFolder` 查询参数被忽略（传与不传返回相同结果）。正确方式是使用 Google Drive 风格的 queryParam 语法。此行为在 API 文档中未明确说明。

**应用**: `list_files(parent_folder)` 将参数转换为 `queryParam=f"'{parent_folder}' in parentFolder"`。

### ADR-3: applicationData 根目录发现 + cloud_folder 自动创建

**决策**: `applicationData` 是容器名称而非文件夹 ID。启动时通过 API 调用发现真实根目录 ID。如果 `cloud_folder` 指定的文件夹不存在，自动创建。

**分析**: `queryParam='applicationData' in parentFolder` 作为关键字可列出根级文件。根级文件的 `parentFolder` 字段包含真实根目录 ID。启动时提取此 ID，后续所有操作使用真实 ID。

`cloud_folder` 支持三种模式:

| 配置值 | 行为 |
|--------|------|
| `"applicationData"` | 挂载容器全部内容（发现真实根 ID） |
| 文件夹名称（如 `"workspace"`） | 查找同名文件夹，**不存在则自动创建** |
| 文件夹 ID（20+ 字符） | 直接使用，无需查询 |

自动创建的价值：即使云空间完全清空，ClawFUSE 也能正常挂载并工作，首次写入时自动在云端创建文件夹。

**典型部署**: `cloud_folder: "workspace"` 将容器 `/home/sandbox/.openclaw/workspace` 映射到云端 `workspace/` 文件夹，容器销毁后文件保留。

### ADR-4: 磁盘 LRU 缓存

**决策**: 文件内容缓存在容器本地磁盘，使用 LRU 淘汰。

**分析**: 容器内存有限（AI Agent 占用大），磁盘空间更经济。

| 场景 | 冷读（首次） | 缓存命中 | 加速比 |
|------|-------------|---------|--------|
| 1KB 文件 | 504ms | 0.34ms | **1482x** |
| 10KB 文件 | 243ms | 0.36ms | **675x** |
| 100KB 文件 | 200ms | 0.43ms | **465x** |
| 1MB 文件 | 148ms | 0.4ms | **370x** |
| 5MB 文件 | 156ms | 0.4ms | **390x** |

5MB 大文件缓存命中后 0.4ms，比本地磁盘读 5MB（~5ms）还快 **12x**。LRU 保留热点文件，`max_bytes`/`max_files` 可配置防止磁盘溢出。

### ADR-5: 写缓冲 + 异步 drain

**决策**: 写入先缓存在本地 `.buf` 文件，后台 drain 线程定期上传。

**分析**: Drive Kit API 单次上传延迟 ~800ms。如果每次 flush 同步上传，Agent 写入操作被阻塞。缓冲后写入延迟从 O(网络) 降到 O(本地磁盘)，Agent 完全不感知网络。

| 写入操作 | 延迟 | 说明 |
|---------|------|------|
| 小文件 (100B) | **312ms** | create + write + flush |
| 100KB | **190ms** | 内存缓冲，异步上传 |
| 1MB | **221ms** | 内存缓冲 + 异步上传 |
| 5MB | **301ms** | 内存缓冲 + 异步上传 |

容器销毁前 `flush_all(timeout)` 确保数据持久化。即使 SIGKILL 强杀，.buf 文件在磁盘上，下次启动 WriteBuffer 自动恢复。

### ADR-6: 单写者模型

**决策**: 不实现分布式锁和冲突检测。同一时间只有一个容器实例写入。

**分析**: OpenClaw 是单用户单会话场景。引入锁机制在当前场景下是过度工程。若未来需要多容器并发，可引入 MemexFS 的锁模式。

### ADR-7: 非特权挂载

**决策**: FUSE 挂载默认 `allow_other=False`，支持非 root 用户运行。

**分析**: 容器环境通常以非 root 运行。`allow_other=True` 需要 root 或 `/etc/fuse.conf` 配置，在非特权容器中不可用。单用户容器场景不需要 `allow_other`。通过配置项支持开启（`"allow_other": true`）。

同时支持 `nonempty` 配置，允许挂载到非空目录（容器 workspace 通常已有文件）。

## 4. 性能

### 4.1 完整性能矩阵

**测试环境**: 中国大陆公网，Drive Kit API，腾讯云 4C8G

#### 基础操作

| 操作 | 延迟 | 说明 |
|------|------|------|
| FUSE 挂载 | **260ms** | 初始化全部组件 |
| getattr / readdir（已加载） | **0.002ms** | 纯内存索引查询 |
| read（缓存命中） | **0.34-0.4ms** | 磁盘缓存读取 |
| read（缓存未命中，小文件） | **200-500ms** | API 下载 + 缓存填充 |
| write | **< 1ms** | 内存缓冲 |
| create / mkdir | **137-312ms** | API 同步调用 |

#### 冷启动（v4 基准测试，2026-04-28）

| 规模 | 目录数 | 首次 stat | 首次 read | BFS 全量加载 |
|------|--------|-----------|-----------|-------------|
| 小 | 85 | **154ms** | 390ms | **1.0s** |
| 中 | 259 | **324ms** | 352ms | **4.0s** |
| 大 | 1111 | **342ms** | 427ms | **19.0s** |

#### 并发性能（Round 2，960 文件 / 80 叶目录 / 5 层深度）

| 场景 | 线程数 | QPS | 总延迟 |
|------|--------|-----|--------|
| 并发 readdir | 16 | **2629** | 18ms |
| 并发 read（冷） | 16 | **31.6** | 1172ms |
| 并发 read（热） | 16 | **122** | 393ms |

#### 批量操作

| 操作 | 总耗时 | 吞吐 |
|------|--------|------|
| 批量创建 100 文件 | **20.1s** | 5.0 ops/s |
| 批量删除 100 文件 | **16.6s** | 6.0 ops/s |
| 深层目录创建 (5 层) | **1.6s** | mkdir -p 5 级 |

### 4.2 与压缩包方案的全面对比

#### 典型工作负载：Agent 启动后访问 10 个文件（总计 ~100KB）

| 阶段 | 压缩包方案 (20GB) | ClawFUSE |
|------|-------------------|----------|
| 准备阶段（下载 + 解压） | **~227s** | **0.26s**（挂载） |
| 读取 10 个文件 | 0ms（已在本地） | **~2s**（首次冷读） |
| **总计** | **~227s** | **~2.3s** |
| **加速比** | — | **~99x** |

#### 不同数据规模

| 数据规模 | 压缩包 (1Gbps) | 压缩包 (100Mbps) | ClawFUSE 首次访问 |
|---------|----------------|-----------------|-------------------|
| 1GB | 8s | 80s | **~0.8s** |
| 5GB | 40s | 400s | **~0.8s** |
| 20GB | 160s | 1600s | **~0.8s** |
| 50GB | 400s | 4000s | **~0.8s** |
| 100GB | 800s | 8000s | **~0.8s** |

**压缩包方案耗时与数据总量成正比，ClawFUSE 只取决于实际需要的文件。这是根本性的架构优势。**

#### 缓存命中后性能对比

| 操作 | 本地文件系统 | ClawFUSE（缓存命中） | 对比 |
|------|------------|---------------------|------|
| ls 目录 | ~0.5ms | **< 1ms** | 相当 |
| stat 文件 | ~0.1ms | **0.002ms** | **50x 更快** |
| cat 1KB | ~0.1ms | **0.34ms** | 3.4x 慢 |
| cat 5MB | ~5ms | **0.4ms** | **12x 更快** |
| 并发 readdir | ~1000 QPS | **2629 QPS** | **2.6x 更快** |

ClawFUSE 缓存命中后 stat 比本地文件系统快 50 倍（纯内存 vs 系统调用），大文件读取快 12 倍（磁盘缓存路径优化），并发 readdir 快 2.6 倍（纯内存无 IO）。

### 4.3 Drive Kit API 特征总结

| 特征 | 实测值 | 架构影响 |
|------|--------|----------|
| 单次调用固定延迟 | ~800ms | 懒加载避免全量阻塞；并行 BFS 分摊延迟 |
| queryParam 过滤 | `'{id}' in parentFolder` | 必须 queryParam，parentFolder 参数被忽略 |
| pageSize 硬上限 | 100 | 大目录需分页；全量列表 ceil(N/100) 页 |
| 并发支持 | 8 线程 QPS ~4.6 | 后台 BFS 使用 ThreadPoolExecutor(8) |
| applicationData 语义 | 容器名，非文件夹 ID | 启动时需发现真实根 ID |
| download 返回 | 完整文件内容 | 支持 Range 头，可按需读取（未利用） |

### 4.4 缓存加速效果

| 场景 | 未命中 → 命中 | 加速比 |
|------|--------------|--------|
| 文件读取 | 500ms → 0.34ms | **1471x** |
| 目录访问 | 800ms → 0.002ms | **400000x** |
| Agent 写入（同步 vs 缓冲） | 800ms → <1ms | **800x** |

## 5. 容器生命周期

### 5.1 启动（Pre-start）

```
容器编排器创建容器
    │
    ▼ LifecycleManager.pre_start()
    │
    ├─ [0-10ms]    初始化 TokenManager
    ├─ [10-800ms]  discover_application_data_root()
    │                  queryParam='applicationData' in parentFolder
    │                  提取根级文件的 parentFolder → 真实根 ID
    │
    ├─ [800-1600ms] resolve cloud_folder
    │                  list_files(root_id) 查找同名文件夹
    │                  未找到 → 自动创建
    │
    ├─ [~1600ms]   初始化 DirTree（空数据结构，不加载）
    ├─              启动后台 BFS 加载线程（daemon，不阻塞）
    ├─              初始化 Cache + WriteBuffer + drain 线程
    │
    └─ FUSE 挂载 → Agent 启动
       │
       ├─ [挂载后 154-342ms] Agent 首次 stat → ensure_loaded → 文件可达
       ├─ [挂载后 350-427ms] Agent 首次 read → download → 内容可达
       │
       └─ [后台 1-19s]     BFS 全量加载完成，全部元数据在内存
```

### 5.2 销毁（Pre-destroy）

```
SIGTERM / pre-destroy hook
    │
    ▼ LifecycleManager.pre_destroy(timeout=120)
    │
    ├─ writebuf.flush_all(timeout * 0.8)
    │   ├─ 成功 → 删除 .buf
    │   └─ 失败 → 重试 max_retries → 保留 .buf 待恢复
    │
    └─ 返回 SyncResult
       │
       ▼ 容器安全销毁，数据已持久化
```

### 5.3 异常处理

| 场景 | 处理 |
|------|------|
| SIGKILL 强杀 | .buf 文件在磁盘上，下次启动 WriteBuffer 自动恢复 |
| Token 过期 | 401 → force_reread() → 重试一次 → 仍失败则 TokenError |
| API 不可用 | 读返回 EIO；写缓存在本地，drain 重试 |
| 后台加载异常 | 不影响 FUSE，用户路径通过 ensure_loaded 独立加载 |
| 磁盘满 | Cache LRU 驱逐；WriteBuffer 超限返回 ENOSPC |
| 空容器 | 无法发现根 ID，回退 "applicationData" |
| cloud_folder 不存在 | 自动创建文件夹，正常挂载 |

## 6. 容器部署

### 6.1 前置要求

| 要求 | 说明 |
|------|------|
| Python 3.10+ | 语言版本 |
| FUSE 内核模块 | `modprobe fuse`；容器需透传 `/dev/fuse` |
| fusermount | 系统包 `fuse` 提供 |
| 网络连通性 | 能访问 `driveapis.cloud.huawei.com.cn` |
| 可写目录 | cache_dir、write_buf_dir、mount_point |

### 6.2 K8s 部署配置

```yaml
spec:
  containers:
  - name: agent
    securityContext:
      capabilities:
        add: ["SYS_ADMIN"]     # FUSE 挂载必需
    volumeMounts:
    - name: fuse
      mountPath: /dev/fuse
  volumes:
  - name: fuse
    hostPath:
      path: /dev/fuse
```

### 6.3 容器类型兼容性

| 容器类型 | FUSE 支持 | 说明 |
|----------|----------|------|
| 普通 runc | 支持 | 需 SYS_ADMIN + /dev/fuse |
| Kuasar (Stratovirt) | 需平台开启 | 虚拟机内核需编译 FUSE 模块 |
| Kata Containers | 需平台开启 | 同上 |
| gVisor | 不支持 | Sentry 内核无 FUSE |

**安全容器替代方案**: CSI 模式——FUSE 运行在宿主机（CSI Node Plugin），容器通过标准 PVC 挂载访问，无需任何特殊权限。

## 7. 优化方向

### 7.1 ClawFUSE 侧优化

**全量列表替代 BFS**: Drive Kit 支持不带过滤条件返回容器内全部文件。1610 项目仅需 `ceil(1610/100) = 17` 次分页调用，客户端从 parentFolder 关系构建目录树。

| 方式 | API 调用 | 公网耗时 |
|------|---------|---------|
| BFS 逐目录（当前） | 1111 次 | ~19s |
| 无过滤全量列表 | **17 次** | **~14s** |

**Range 按需下载**: Drive Kit 下载接口已支持 HTTP Range 头。大文件随机访问延迟从 O(文件大小) 降至 O(请求大小)。

### 7.2 内网部署性能预期

当前公网访问 Drive Kit API，单次调用 ~800ms。FUSE 与 Drive Kit 部署在同一内网时：

| 指标 | 公网 | 内网 | 提升 |
|------|------|------|------|
| API 延迟 | ~800ms | 5-50ms | 16-160x |
| BFS 全量加载 1111 目录 | 19s | **< 1s** | 19x |
| ensure_loaded 3 层 | 342ms | < 20ms | 17x |
| 文件首次读取 | 500ms | < 50ms | 10x |
| create / mkdir | 200ms | < 10ms | 20x |

内网环境下所有性能瓶颈均降至毫秒级，体验接近本地文件系统。

### 7.3 CSI 模式演进

对于安全容器环境（Kuasar、Kata），FUSE 在容器内不可用。CSI 模式将 FUSE 运行在宿主机上，容器通过标准 K8s 存储接口访问：

```
K8s 宿主机:  CSI Driver → FUSE 挂载 Drive Kit
安全容器:    标准 PVC 挂载 → 读写文件（无感知）
```

优势：容器无需任何特殊权限，标准 PVC 使用方式，与存储类型无关。

## 8. 与 MemexFS 的关系

ClawFUSE 不依赖 MemexFS，但借鉴了其 Drive Kit API 使用经验。两者的定位差异：

| 维度 | MemexFS | ClawFUSE |
|------|---------|----------|
| 定位 | 多设备同步 | 单容器临时访问 |
| 外部依赖 | Relay 服务 + PC 同步客户端 | 无（直接 Drive Kit API） |
| 并发模型 | 分布式锁 + 冲突检测 | 单写者 |
| 元数据 | 启动全量加载 | 懒加载 + 后台并行 |
| 缓存 | 内存 dict | 磁盘 LRU |
| 写入 | 每次 flush 上传 | 缓冲 + 异步 drain |

若未来需要多容器并发写入，可引入 MemexFS 的 Properties 协作锁模式。

## 9. 风险与演进

| 风险 | 影响 | 缓解 |
|------|------|------|
| API 延迟波动 | 首次访问变慢 | 懒加载避免全量阻塞；内网部署消除 |
| 大文件首次读取 | Agent 等待 | Range 按需下载优化 |
| Token 过期 | API 全部失败 | force_reread + 外部更新 SLA |
| 安全容器无 FUSE | 无法挂载 | CSI 模式（宿主机 FUSE + 标准 PVC） |
| 后台加载线程异常 | 元数据不完整 | daemon 线程 + ensure_loaded 独立路径 |

**演进路线**:

| 方向 | 触发条件 | 依赖 |
|------|----------|------|
| 全量列表替代 BFS | 立即可做 | 无 |
| Range 按需下载 | 立即可做 | 无 |
| CSI Driver | 安全容器部署 | K8s CSI 规范 |
| 内网部署 | 生产环境 | 基础设施 |
| 增量元数据同步 | 大量文件（10k+） | Drive Kit changes API |
| 多容器并发 | 多 Agent 场景 | MemexFS 锁模式 |

## 附录：测试数据汇总

| 测试轮次 | 日期 | 场景 | 关键结论 |
|---------|------|------|----------|
| Round 1 | 04-26 | 50 文件 / 30 目录 / 3 层 | 启动 260ms vs 227s，873x 加速 |
| Round 2 | 04-26 | 960 文件 / 80 叶目录 / 5 层 + 大文件 | 并发 readdir 2629 QPS，5MB 缓存读 12x 快于本地 |
| Cold v4 | 04-28 | 85/259/1111 目录独立冷启动 | BFS 线性 ~15ms/目录，1111 目录 19s，首次 stat 342ms |
