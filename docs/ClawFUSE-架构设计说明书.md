# ClawFUSE 架构设计说明书

> 版本: 1.0 | 日期: 2026-04-24
> 项目: ClawFUSE — OpenClaw 容器 Drive Kit FUSE 挂载

## 1. 系统概述

### 1.1 项目背景

OpenClaw 是运行在云侧的 AI Agent 容器，为用户提供智能文件管理和处理能力。容器具有以下特征：

- **临时性**：不活跃一段时间后自动销毁，用户返回时重新创建
- **无状态**：容器本身不持久化用户数据，所有用户数据存储在华为 Drive Kit 云空间
- **按需启动**：容器创建时需要快速获得用户全部文件的访问能力

**核心问题**：如何在容器启动时快速让 Agent 访问到用户在云空间的全部文件？

### 1.2 设计目标

| 目标 | 说明 |
|------|------|
| **快速启动** | 容器启动后秒级可用，不需要等待全量文件下载 |
| **透明访问** | Agent 通过本地文件系统接口（POSIX）访问云文件，无需感知底层云存储 |
| **写回持久化** | Agent 写入的文件自动同步到云空间，容器销毁后数据不丢失 |
| **资源可控** | 缓存大小可配置，LRU 淘汰防止容器磁盘溢出 |
| **独立部署** | 不依赖 MemexFS 或其他外部服务，容器内自包含 |

### 1.3 术语表

| 术语 | 说明 |
|------|------|
| ClawFUSE | 本项目名称，将 Drive Kit 云存储挂载为 FUSE 文件系统 |
| OpenClaw | 云侧 AI Agent 容器 |
| Drive Kit | 华为云存储服务 REST API |
| FUSE | Filesystem in Userspace，用户态文件系统框架 |
| DirTree | 内存中的目录树结构，仅包含元数据 |
| Cache | 磁盘上的文件内容缓存 |
| WriteBuffer | 写缓冲区，暂存未同步到 Drive Kit 的文件修改 |
| Drain | 后台线程将写缓冲区的内容上传到 Drive Kit |

### 1.4 参考文档

| 文档 | 说明 |
|------|------|
| 华为 Drive Kit REST API 文档 | 华为开发者官网 |
| MemexFS 架构设计说明书 v2.5 | Drive Kit FUSE 挂载的前序项目（参考模式，非依赖） |
| MemexFS 详细设计说明书 | 同步协议、锁机制、缓存策略参考 |
| fusepy 文档 | Python FUSE 绑定 |

## 2. 总体架构

### 2.1 架构总览

```
┌──────────────────────────────────────────────────┐
│              OpenClaw 容器（临时）                  │
│                                                    │
│  Agent App                                         │
│     │                                              │
│     │ read / write / list                          │
│     ▼                                              │
│  /mnt/drive/  ◄── FUSE mount point                │
│     │                                              │
│     │ FUSE operations                              │
│     ▼                                              │
│  ┌──────────────────────────────────────────────┐  │
│  │              ClawFUSE Core                    │  │
│  │                                               │  │
│  │  ┌─────────┐  ┌─────────┐  ┌──────────────┐ │  │
│  │  │ DirTree │  │  Cache  │  │ WriteBuffer   │ │  │
│  │  │ (内存)  │  │ (磁盘)  │  │ (磁盘+线程)  │ │  │
│  │  └────┬────┘  └────┬────┘  └──────┬───────┘ │  │
│  │       │            │              │          │  │
│  │       └────────────┼──────────────┘          │  │
│  │                    │                         │  │
│  │             ┌──────▼──────┐                  │  │
│  │             │ DriveKit    │                  │  │
│  │             │ Client      │                  │  │
│  │             └──────┬──────┘                  │  │
│  └────────────────────┼────────────────────────┘  │
│                       │                            │
│              TokenManager                          │
│              (只读 token 文件)                      │
└───────────────────────┼────────────────────────────┘
                        │ HTTPS
                        ▼
              ┌──────────────────┐
              │  Drive Kit Cloud │
              │  (华为云存储)    │
              └──────────────────┘
```

### 2.2 数据流

#### 读取流程

```
Agent read("/data/report.csv")
    │
    ▼
FUSE.read()
    │
    ├─ DirTree.resolve("/data/report.csv") → FileMeta
    │
    ├─ Cache.get(file_id)
    │   ├─ 命中 → 返回缓存的 bytes
    │   └─ 未命中 → DriveKitClient.download(file_id)
    │                → Cache.put(file_id, content)
    │                → 返回 content
    │
    └─ 返回 content[offset:offset+size]
```

#### 写入流程

```
Agent write("/data/output.json", content)
    │
    ▼
FUSE.write()
    │  内存缓冲区暂存
    │
    ▼
FUSE.flush() / FUSE.release()
    │
    ▼
WriteBuffer.enqueue(file_id, content)
    │  写入本地 .buf 文件（crash safety）
    │
    ▼ 后台 drain 线程（每 5 秒）
    │
DriveKitClient.update_file(file_id, content)
    │  上传成功 → 删除 .buf 文件
    │  上传失败 → 重试（最多 3 次）
```

### 2.3 组件关系

| 组件 | 职责 | 存储 | 线程 |
|------|------|------|------|
| DirTree | 目录结构 + 元数据索引 | 内存 | 主线程 + TTL 刷新 |
| Cache | 文件内容缓存 | 磁盘 | 主线程（FUSE 回调） |
| WriteBuffer | 写缓冲 + 后台上传 | 磁盘 | 后台 drain 线程 |
| DriveKitClient | API 调用封装 | 无 | 调用方线程 |
| TokenManager | Token 读取 | 内存缓存 | 主线程 |
| FUSE Ops | 文件系统接口 | 内存句柄 | FUSE 线程池 |

### 2.4 与 MemexFS 的区别

| 维度 | MemexFS | ClawFUSE |
|------|---------|----------|
| 场景 | 多设备同步（PC+容器+手机） | 单设备临时容器 |
| Relay 服务 | 必需（信令枢纽） | 不需要 |
| PC 同步客户端 | 必需 | 不需要 |
| 分布式锁 | Properties 协作锁 | 不需要（单写者） |
| 事件系统 | events.json + webhook | 不需要 |
| trigger-sync | FUSE→Relay→PC 上传 | 直接 Drive Kit API |
| pc:// 哨兵 | PC 独有文件标识 | 不需要 |
| 缓存 | 内存 dict | 磁盘 LRU |
| Token 管理 | OAuth refresh_token | 只读 access_token 文件 |
| 写入策略 | 每次 flush 上传 | 缓冲 + 后台 drain |
| 冲突处理 | SHA-256 + 历史版本保留 | 不需要（单写者） |

## 3. 核心设计决策（ADR）

### ADR-001: 目录全量 + 文件按需加载

**决策**：启动时加载全部文件/目录的元数据（名称、大小、类型），不下载文件内容。文件内容仅在首次读取时按需下载。

**Why**: 容器需要快速启动（目标 <3s/1000 文件）。元数据是小 JSON（每个文件约 200 字节），1000 文件总计约 200KB，通过 Drive Kit list API 分页拉取约需 2-3 秒。如果全量下载文件内容，GB 级数据可能需要分钟级等待，且大量文件可能永远不会被访问。

**How to apply**: DirTree 在启动时通过 Drive Kit list API 加载全量元数据；Cache 仅在 read 时触发下载。

### ADR-002: 磁盘缓存 + LRU 淘汰

**决策**：文件内容缓存在容器本地磁盘，使用 LRU（最近最少使用）算法淘汰。

**Why**: 容器内存有限（AI Agent 本身占内存较多），缓存大文件（数据集、模型等）会挤压 Agent 可用内存。磁盘空间虽然也有限，但更便宜，且 LRU 淘汰可确保常用文件保留。

**How to apply**: Cache 模块将文件内容写入 `{cache_dir}/{prefix}/{file_id}.content`，元信息写入 `.meta` sidecar。`OrderedDict` 维护 LRU 顺序，超出 `max_bytes` 时淘汰最早条目。

### ADR-003: 写缓冲 + 后台 drain

**决策**：文件写入先缓存在本地磁盘（`.buf` 文件），后台线程定期批量上传到 Drive Kit，而非每次写入立即上传。

**Why**: Agent 可能频繁写入小文件或持续追加写入。每次 flush 都调 Drive Kit API 上传会带来：(1) 写入延迟高（网络 RTT 50-200ms）影响 Agent 性能；(2) API 调用频率过高可能触发限流；(3) 网络瞬时不可用时写入失败。缓冲模式将写入延迟从 O(网络) 降到 O(本地磁盘)。

**How to apply**: WriteBuffer 模块维护 pending writes 队列，后台 drain 线程每 `drain_interval` 秒上传一批。容器销毁前调用 `flush_all()` 确保所有写入持久化。

### ADR-004: Token 只读模式

**决策**：ClawFUSE 不实现 OAuth token 刷新流程，仅从文件读取 access_token。Token 的获取和刷新由外部系统负责。

**Why**: 容器环境不应持有 refresh_token 等长期凭据（安全风险）。Token 由外部编排系统在容器启动前写入，过期时外部系统更新文件，ClawFUSE 检测到 401 后重新读取。

**How to apply**: TokenManager 仅从指定路径读取 token 文件。401 错误时重新读取文件（外部可能已更新），不调用 OAuth refresh API。

### ADR-005: 单写者模型

**决策**：不实现分布式锁和冲突检测，假设同一时间只有一个容器实例写入用户的 Drive Kit 空间。

**Why**: OpenClaw 是单用户单会话场景——一个用户同一时间只激活一个容器实例。多设备并发写入的场景由 MemexFS 处理，不属于 ClawFUSE 职责。引入锁机制会显著增加复杂度（Properties 锁 + 心跳 + 超时回收），在当前场景下是过度工程。

**How to apply**: 写入直接调 Drive Kit update API，不获取锁。如果未来需要多容器并发，可引入 MemexFS 的 Properties 协作锁模式。

## 4. 模块划分

### 4.1 模块总览

```
clawfuse/
├── config.py      ─── 环境变量配置（frozen dataclass）
├── token.py       ─── Token 读取（只读文件）
├── client.py      ─── Drive Kit REST API 客户端
├── dirtree.py     ─── 目录树加载与路径解析
├── cache.py       ─── 磁盘 LRU 文件内容缓存
├── writebuf.py    ─── 写缓冲 + 后台 drain
├── fuse.py        ─── FUSE 文件系统操作
├── lifecycle.py   ─── 容器生命周期管理
├── mount.py       ─── CLI 入口
└── exceptions.py  ─── 自定义异常层级
```

### 4.2 模块职责

#### config.py

从环境变量加载所有配置，不可变（frozen dataclass）。启动时验证必要参数。

关键配置项：
- `token_file`: access_token 文件路径
- `mount_point`: FUSE 挂载点
- `root_folder`: Drive Kit 根文件夹 ID
- `cache_dir`, `cache_max_mb`, `cache_max_files`: 缓存配置
- `write_buf_dir`, `drain_interval`: 写缓冲配置

#### token.py

从文件读取 access_token，缓存到内存。401 时重新读取（外部可能已更新）。

#### client.py

精简版 Drive Kit REST API 客户端，仅保留本项目需要的接口：
- 文件 CRUD：create, update, get, download, delete
- 文件夹：create_folder
- 列表：list_files（分页）
- 401 自动重试（重新读取 token）

不包含：lock, events, batch, search, subscribe, history versions。

#### dirtree.py

启动时加载全量目录元数据（无文件内容），构建内存路径树。支持：
- 路径 → FileMeta 解析
- 列举目录内容
- 动态增删改条目（对应 create/unlink/mkdir/rmdir/rename）
- TTL 过期后自动从 Drive Kit 刷新

#### cache.py

磁盘文件内容缓存：
- LRU 淘汰（OrderedDict）
- 双文件存储：`.content` + `.meta` sidecar
- 启动时从磁盘重建索引（缓存持久化）
- 原子写入（先写临时文件再 rename）

#### writebuf.py

写缓冲 + 后台上传：
- 写入立即持久化到 `.buf` 文件（crash safety）
- 后台 drain 线程定期上传
- 失败重试（最多 3 次）
- `flush_all()` 同步排空（pre-destroy 调用）
- 启动时恢复未完成的写入

#### fuse.py

FUSE 文件系统操作类，实现 fusepy 的 Operations 接口：
- 文件操作：open, read, write, create, flush, release, unlink, truncate
- 目录操作：readdir, mkdir, rmdir, rename
- 元数据：getattr
- 辅助：statfs, access, chmod, chown, utimens（no-op）

#### lifecycle.py

容器生命周期管理：
- `pre_start()`: 验证 token → 加载目录树 → 恢复缓存 → 挂载 FUSE → 启动 drain
- `pre_destroy()`: 停止 drain → flush_all → 验证上传 → 卸载 FUSE

#### mount.py

CLI 入口点，组装所有组件并启动挂载。注册 SIGTERM 信号处理触发 pre_destroy。

#### exceptions.py

异常层级：ClawFUSEError → DriveKitError / TokenError / CacheError / SyncError / MountError / ConfigError

## 5. 性能目标

| 场景 | 指标 | 目标值 |
|------|------|--------|
| 启动挂载（1000 文件） | 端到端耗时 | < 3s |
| 启动挂载（10000 文件） | 端到端耗时 | < 15s |
| 首次读文件（cache miss） | 延迟 | 取决于文件大小和网络 |
| 缓存命中读文件 | 延迟 | < 10ms |
| 写入文件（enqueue） | 延迟 | < 5ms |
| 后台 drain（单文件上传） | 延迟 | 取决于文件大小和网络 |
| Pre-destroy 同步（10 pending） | 总耗时 | < 30s |
| LRU 淘汰 | 正确性 | 超出 max_bytes 时淘汰最久未访问 |
| 并发读（10 线程） | 正确性 | 无 deadlock |
| 大文件读（100MB） | 内存 | 不超过缓冲区大小 |

### 性能关键路径

1. **启动性能**：主要受 Drive Kit list API 分页速度限制。1000 文件约需 5 页请求（pageSize=200），API 延迟约 200-500ms/页。
2. **读取延迟**：首次读取需要 Drive Kit download API 调用（100-500ms + 文件大小/带宽）。缓存命中时只需本地磁盘 I/O（<10ms）。
3. **写入延迟**：enqueue 操作只写本地磁盘（<5ms），实际上传延迟由 drain 线程承担。

## 6. 容器生命周期集成

### 6.1 启动流程（Pre-start）

```
容器编排器创建容器
    │
    ├─ 1. 外部写入 access_token 文件
    ├─ 2. 创建挂载点目录 /mnt/drive
    │
    ▼
ClawFUSE 启动
    │
    ├─ 3. 读取 token 文件
    ├─ 4. 调 Drive Kit list API 加载目录树
    ├─ 5. 恢复磁盘缓存（如有）
    ├─ 6. 恢复写缓冲（如有崩溃的 pending writes）
    ├─ 7. 挂载 FUSE 到 /mnt/drive
    ├─ 8. 启动 drain 线程
    │
    ▼
OpenClaw Agent 启动，访问 /mnt/drive
```

### 6.2 销毁流程（Pre-destroy）

```
容器编排器决定销毁容器
    │
    ├─ 发送 SIGTERM（或调用 pre-destroy hook）
    │
    ▼
ClawFUSE pre_destroy()
    │
    ├─ 1. 停止 drain 线程
    ├─ 2. flush_all(): 同步上传所有 pending writes
    ├─ 3. 验证上传完整性（sha256 校验，可选）
    ├─ 4. 卸载 FUSE
    │
    ▼
容器安全销毁，数据已持久化到 Drive Kit
```

### 6.3 异常场景

| 场景 | 处理 |
|------|------|
| 容器被 kill（SIGKILL） | .buf 文件在磁盘上，下次启动时恢复 |
| Token 过期 | 401 时重新读取 token 文件，外部负责更新 |
| Drive Kit API 不可用 | 读操作返回 EIO；写操作缓存在本地，drain 重试 |
| 磁盘满 | 缓存驱逐至最低，写缓冲超限时返回 ENOSPC |
| drain 线程上传失败 | 重试 3 次，仍失败则保留 .buf 文件等待下次恢复 |

## 7. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Drive Kit list API 对大量文件（10k+）启动慢 | 启动时间超标 | pageSize=200 分页 + 并行请求不同层级 |
| 容器在 flush 前被强制杀死 | 数据丢失 | .buf 文件 crash safety + SIGTERM hook |
| 缓存淘汰活跃文件 | 性能下降 | LRU 保留热点文件 + 可配置 max_bytes |
| Token 过期未及时更新 | API 调用失败 | 401 重读文件 + 外部 SLA 确保 token 更新 |
| FUSE 阻塞 API 调用 | Agent 操作卡住 | 读超时 30s + 写缓冲化 + 目录树 TTL |
| Drive Kit 最终一致性 | 新建文件在列表中不可见 | 本地缓存 30s grace period |

## 8. 未来演进

| 演进方向 | 说明 | 触发条件 |
|---------|------|---------|
| 预热策略 | 启动时预下载高频文件 | Agent 读取模式可预测 |
| 多容器并发 | 引入 Properties 协作锁 | 用户需要多个 Agent 同时工作 |
| Watchdog 变更检测 | 定期轮询 Drive Kit 变更 | 需要感知外部修改 |
| P2P 文件传输 | 容器间直接传输避免下载 | 多容器协作场景 |
| 增量目录刷新 | 仅同步变化的元数据 | 大量文件场景下降低启动延迟 |
