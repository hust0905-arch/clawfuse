# ClawFUSE 架构设计说明书

> 版本: 2.0 | 日期: 2026-04-26
> 项目: ClawFUSE — OpenClaw 容器 Drive Kit FUSE 挂载

---

## 1. 概述

### 1.1 问题定义

OpenClaw 是运行在云侧的 AI Agent 容器，用户数据存储在华为 Drive Kit 云空间。容器具有临时性——不活跃后自动销毁，用户返回时重建。

核心挑战：**容器启动后，Agent 需要在数秒内访问到用户在云空间的全部文件，且写入的文件必须在容器销毁后持久化。**

传统方案（压缩包全量同步）存在启动慢（20GB 数据需 33 分钟下载解压）、全量传输、不支持增量等问题，无法满足 AI Agent 快速启动的需求。

### 1.2 解决方案

ClawFUSE 将 Drive Kit 云存储挂载为本地 FUSE 文件系统。Agent 通过标准 POSIX 接口（open、read、write、readdir）访问云文件，无需感知底层存储。

设计原则：**元数据懒加载 + 后台并行预加载 + 文件内容按需加载 + 写入异步回传。**

- 挂载启动不阻塞（< 10ms），后台线程并行加载元数据
- 用户访问未加载目录时优先处理，路径级按需加载
- 文件读取走 LRU 磁盘缓存，写入先缓冲到本地再异步上传

### 1.3 设计目标与达成

| 目标 | 指标 | 实测达成 |
|------|------|----------|
| 秒级启动 | 挂载后立即可用 | **< 10ms**（初始化组件即返回） |
| 透明访问 | POSIX 全覆盖 | getattr/readdir/read/write/create/mkdir/unlink/rename |
| 写回持久化 | 容器销毁后数据不丢失 | WriteBuffer + flush_all 保证 |
| 资源可控 | 缓存上限可配置 | LRU 磁盘缓存，默认 512MB |
| 零外部依赖 | 容器内自包含 | 仅需 Drive Kit API 访问权限 |

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

#### 读取路径

```
Agent read(path, offset, size)
    │
    ├─ ensure_loaded(parent_dir)          ← 路径级懒加载
    │   ├─ 已加载 → 跳过 (0ms)
    │   └─ 未加载 → load_dir API (~800ms)
    │
    ├─ DirTree.resolve(path) → FileMeta   ← 纯内存
    │
    └─ Cache.get(file_id)
        ├─ 命中 → 返回 (0.2ms)
        └─ 未命中 → download → cache → 返回 (0.8-1.5s)
```

#### 写入路径

```
Agent write(path, data, offset)
    │
    ├─ 内存 bytearray 缓冲 (< 1ms)        ← 切片赋值，避免 O(n²)
    │
    ├─ flush → WriteBuffer.enqueue        ← 持久化 .buf 文件
    │   └─ 同时更新 Cache（后续读可见）
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

**分析**: Drive Kit 单次 API 调用有 ~800ms 固定延迟。若启动时串行 BFS 加载 1110 个目录，需 ~430 秒——Agent 无法接受。即使 8 线程并行 BFS 仍需 ~20 秒阻塞。

懒加载的关键洞察：**Agent 从根目录开始逐级深入访问文件，实际只需要路径上的 2-3 个目录即可开始工作。** 后台线程同时加载其余目录，Agent 几乎无感。

**实测数据（1110 目录 + 500 文件，2026-04-26，中国大陆公网）**:

| 操作 | 延迟 |
|------|------|
| FUSE 挂载 | **< 10ms** |
| 用户首次访问根目录（load_dir root） | **~800ms** |
| 深入 3 级目录（ensure_loaded 3 层） | **~2.4s** |
| 后台 8 线程 BFS 全部加载完成 | **~20s** |
| 加载完成后 getattr/readdir | **< 0.02ms**（纯内存） |

**典型 Agent 启动时间线**:

```
0.0s    FUSE 挂载完成
0.8s    Agent ls /mnt/drive/           → load_dir(root)
1.6s    Agent 进入 workspace/          → load_dir(workspace)
2.6s    Agent 读取第一个文件            → download + cache
        ... Agent 开始正常工作 ...
~20s    后台加载完成，全部元数据在内存中
```

**对比压缩包方案**: 压缩包下载 20GB 数据需 33 分钟。ClawFUSE 从挂载到 Agent 读取第一个文件仅需 ~2.6 秒，**加速 760 倍**。

### ADR-2: Drive Kit queryParam 过滤

**决策**: 列表接口使用 `queryParam='{folderId}' in parentFolder` 过滤，不使用 `parentFolder` 直接参数。

**分析**: 实测发现 Drive Kit API 的 `parentFolder` 查询参数被忽略（传与不传返回相同结果）。正确方式是使用 Google Drive 风格的 queryParam 语法。此行为在 API 文档中未明确说明。

**应用**: `list_files(parent_folder)` 将参数转换为 `queryParam=f"'{parent_folder}' in parentFolder"`。

### ADR-3: applicationData 根目录发现

**决策**: `applicationData` 是容器名称而非文件夹 ID。启动时通过 API 调用发现真实根目录 ID。

**分析**: `queryParam='applicationData' in parentFolder` 作为关键字可列出根级文件。根级文件的 `parentFolder` 字段包含真实根目录 ID（如 `CjFH_1gNMlfHzhobAATwzTImCByn0TJfx`）。启动时提取此 ID，后续所有操作使用真实 ID。

`cloud_folder` 支持三种模式:

| 配置值 | 行为 |
|--------|------|
| `"applicationData"` | 挂载容器全部内容（发现真实根 ID） |
| 文件夹名称（如 `"workspace"`） | 发现根 ID → 查找同名文件夹 → 挂载其内容 |
| 文件夹 ID（20+ 字符） | 直接使用，无需查询 |

**典型部署**: `cloud_folder: "workspace"` 将容器 `/home/sandbox/.openclaw/workspace` 映射到云端 `workspace/` 文件夹，容器销毁后文件保留。

### ADR-4: 磁盘 LRU 缓存

**决策**: 文件内容缓存在容器本地磁盘，使用 LRU 淘汰。

**分析**: 容器内存有限（AI Agent 占用大），磁盘空间更经济。缓存命中后读取 ~0.2ms，未命中 ~1s，**加速 5000 倍**。LRU 保留热点文件，`max_bytes`/`max_files` 可配置防止磁盘溢出。

### ADR-5: 写缓冲 + 异步 drain

**决策**: 写入先缓存在本地 `.buf` 文件，后台 drain 线程定期上传。

**分析**: Drive Kit API 单次上传延迟 ~800ms。如果每次 flush 同步上传，Agent 写入操作被阻塞。缓冲后写入延迟从 O(网络) 降到 O(本地磁盘) < 1ms，Agent 完全不感知网络。容器销毁前 `flush_all(timeout)` 确保数据持久化。

### ADR-6: 单写者模型

**决策**: 不实现分布式锁和冲突检测。同一时间只有一个容器实例写入。

**分析**: OpenClaw 是单用户单会话场景。引入锁机制（Properties 协作锁 + 心跳 + 超时回收）在当前场景下是过度工程。若未来需要多容器并发，可引入 MemexFS 的锁模式。

## 4. 性能

### 4.1 实测性能矩阵

**测试环境**: 中国大陆公网，Drive Kit API，1110 目录 + 500 文件

| 操作 | 延迟 | 说明 |
|------|------|------|
| getattr / readdir（已加载） | **< 0.02ms** | 纯内存索引查询 |
| read（缓存命中） | **~0.2ms** | 磁盘缓存读取 |
| read（缓存未命中） | **0.8-1.5s** | API 下载 + 缓存填充 |
| write | **< 1ms** | 内存缓冲 |
| create / mkdir | **0.6-1.0s** | API 同步调用 |
| FUSE 挂载 | **< 10ms** | 仅初始化组件 |
| 后台全量加载（8 线程 BFS） | **~20s** | 1110 目录并行加载 |
| 首次访问未加载目录 | **~0.8s/级** | ensure_loaded 逐级加载 |

### 4.2 性能瓶颈分析

| 瓶颈场景 | 延迟 | 根因 | 实际影响评估 |
|----------|------|------|-------------|
| 深层目录首次访问（> 3 级） | > 2.4s | ensure_loaded 逐级加载，每级一次 API | **低**: OpenClaw workspace 通常 2-3 层，Agent 很少访问超深路径 |
| 大目录首次 readdir（> 100 子项） | > 0.8s | pageSize 上限 100，需多次分页 | **低**: workspace 子目录极少超 100 项 |
| 大文件首次读取 | 数秒~数十秒 | 全量下载，受带宽限制 | **中**: 仅首次，后续走缓存；内网部署后显著改善 |
| create/mkdir 延迟 | 0.6-1.0s/次 | API 固定延迟 | **低**: Agent 创建文件不频繁，且不阻塞其他操作 |

### 4.3 Drive Kit API 特征总结

实测揭示的 API 行为特征，对架构决策有直接影响：

| 特征 | 实测值 | 架构影响 |
|------|--------|----------|
| 单次调用固定延迟 | ~800ms | 懒加载避免全量阻塞；并行 BFS 分摊延迟 |
| queryParam 过滤 | `'{id}' in parentFolder` | 必须 queryParam，parentFolder 参数被忽略 |
| pageSize 硬上限 | 100 | 大目录需分页；全量列表 ceil(N/100) 页 |
| 并发支持 | 8 线程 QPS ~4.6 | 后台 BFS 使用 ThreadPoolExecutor(8) |
| parentFolder 格式 | str 或 dict | 代码兼容两种格式 |
| applicationData 语义 | 容器名，非文件夹 ID | 启动时需发现真实根 ID |
| download 返回 | 完整文件内容 | 支持 Range 头，可实现按需读取（未利用） |

### 4.4 缓存加速效果

| 场景 | 未命中 → 命中 | 加速比 |
|------|--------------|--------|
| 文件读取 | 1s → 0.2ms | **5000x** |
| 目录访问 | 800ms → 0.02ms | **40000x** |
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
    ├─ [800-1600ms] 如果 cloud_folder 为名称
    │                  list_files(root_id) 查找同名文件夹
    │
    ├─ [~1600ms]   初始化 DirTree（空数据结构，不加载）
    ├─              启动后台 BFS 加载线程（daemon，不阻塞）
    ├─              初始化 Cache + WriteBuffer + drain 线程
    │
    └─ 返回 MountResult(success=True, load_time≈0.8s)
       │
       ▼ FUSE 挂载 → Agent 启动
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
| cloud_folder 不存在 | MountError，列出可用文件夹 |

## 6. 优化方向

### 6.1 ClawFUSE 侧优化（不需要 API 改动）

**全量列表替代 BFS**: 当前 `background_full_load` 对每个目录单独调用 `list_files`（1110 目录 = 1110 次调用）。Drive Kit 支持不带过滤条件返回容器内全部文件——1610 项目仅需 `ceil(1610/100) = 17` 次分页调用，客户端从 parentFolder 关系构建目录树。

| 方式 | API 调用 | 公网耗时 | 内网耗时 |
|------|---------|---------|---------|
| BFS 逐目录（当前） | 1110 次 | ~20s（8 线程并行） | ~4s |
| 无过滤全量列表 | **17 次** | ~14s（串行分页） | **~0.5s** |

预计改动 ~50 行代码，无风险。

**Range 按需下载**: Drive Kit 下载接口已支持 HTTP Range 头。当前 `download_file` 全量下载再切片，100MB 文件读 4KB 也要下载全量。改为 Range 请求后，大文件随机访问延迟从 O(文件大小) 降至 O(请求大小)。

### 6.2 Drive Kit API 改进建议

**增量变更查询（高优先级）**: 新增 `changes` 接口，返回上次查询后的变更（新增/删除/修改）。容器重启时仅同步增量，无变更时 0 次 API 调用。对容器频繁创建/销毁的 OpenClaw 场景价值最大。

**pageSize 上限提升**: 当前硬上限 100，元数据 JSON 每条仅 ~200 字节，100 条 ~20KB。提升至 500-1000 可将分页调用减少 5-10 倍，传输量增加可忽略。

**变更推送通知**: WebSocket/Webhook 推送文件变更事件，替代轮询刷新。支持实时感知外部修改（用户在 PC 端修改，Agent 在容器内可见）。

**其他**: 文档明确 queryParam 语法（parentFolder 被忽略的行为未说明）、parentField 字段格式统一（str vs dict）、applicationData 根 ID 直接返回。

### 6.3 内网部署性能预期

当前公网访问 Drive Kit API（中国大陆），单次调用 ~800ms。FUSE 与 Drive Kit 部署在同一内网或同机房时：

| 指标 | 公网 | 内网 | 提升 |
|------|------|------|------|
| API 延迟 | ~800ms | 5-50ms | 16-160x |
| 全量元数据加载（17 页） | ~14s | **~0.5s** | 28x |
| ensure_loaded 3 层 | ~2.4s | < 0.15s | 16x |
| 文件首次读取 | ~1s | < 0.1s | 10x |
| create / mkdir | 0.6-1.0s | < 50ms | 12-20x |

内网环境下，所有性能瓶颈（深层目录、大文件、大目录分页）均降至毫秒级，用户体验接近本地文件系统。**建议生产环境优先考虑 Drive Kit API 就近部署。**

## 7. 与 MemexFS 的关系

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

## 8. 风险与演进

| 风险 | 影响 | 缓解 |
|------|------|------|
| API 延迟波动 | 首次访问变慢 | 懒加载避免全量阻塞；内网部署消除 |
| 大文件首次读取 | Agent 等待 | Range 按需下载优化 |
| Token 长期不更新 | API 全部失败 | force_reread + 外部更新 SLA |
| 后台加载线程异常 | 元数据不完整 | daemon 线程 + ensure_loaded 独立路径 |

**演进路线**:

| 方向 | 触发条件 | 依赖 |
|------|----------|------|
| 全量列表替代 BFS | 立即可做 | 无 |
| Range 按需下载 | 立即可做 | 无 |
| 增量元数据同步 | 大量文件（10k+） | Drive Kit changes API |
| 内网部署 | 生产环境 | 基础设施 |
| 多容器并发 | 多 Agent 场景 | MemexFS 锁模式 |
