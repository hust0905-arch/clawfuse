# ClawFUSE

将华为 Drive Kit 云存储挂载为本地 FUSE 文件系统，为 OpenClaw 容器内的 AI Agent 提供透明读写能力。

## 架构

```
┌──────────────────────────────────────────────────────────┐
│                    容器 (OpenClaw)                        │
│                                                          │
│  Agent ──► /mnt/drive/  ──► ClawFUSE                     │
│                               │                          │
│                    ┌──────────┼──────────┐                │
│                    │          │          │                │
│                DirTree    Cache     WriteBuffer           │
│               (元数据)    (读缓存)   (写缓冲)              │
│                    │          │          │                │
│                    └──────────┼──────────┘                │
│                               │                          │
│                         DriveKitClient                    │
│                               │                          │
└───────────────────────────────┼──────────────────────────┘
                                │  HTTPS
                                ▼
                     ┌─────────────────────┐
                     │   Drive Kit 云端    │
                     │  (华为云)            │
                     └─────────────────────┘
```

**设计原则:** 元数据懒加载 + 后台并行预加载 + 文件内容按需加载 + 写入异步回传。挂载启动不阻塞，后台线程并行加载全部目录元数据；用户访问未加载目录时优先处理。文件读取走 LRU 磁盘缓存；写入先缓冲到本地，后台异步上传。

## 快速开始

### 1. 安装

```bash
pip install -e ".[fuse]"
```

### 2. 创建配置文件

复制 `clawfuse.json.example` 为 `clawfuse.json`，填入 Drive Kit 访问令牌：

```json
{
  "token": "你的_ACCESS_TOKEN",
  "cloud_folder": "workspace",
  "mount_point": "/home/sandbox/.openclaw/workspace"
}
```

### 3. 挂载

```bash
clawfuse --config clawfuse.json
```

## 配置说明

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `token` | string | **必填** | Drive Kit OAuth 访问令牌 |
| `cloud_folder` | string | `"applicationData"` | 云端文件夹名称或 ID |
| `mount_point` | string | `"/mnt/drive"` | 本地 FUSE 挂载点 |
| `cache_dir` | string | `"/tmp/clawfuse-cache"` | 磁盘缓存目录 |
| `cache_max_mb` | int | `512` | 缓存上限 (MB) |
| `cache_max_files` | int | `500` | 最大缓存文件数 |
| `write_buf_dir` | string | `"/tmp/clawfuse-writes"` | 写缓冲目录 |
| `drain_interval` | float | `5.0` | 后台上传间隔 (秒) |
| `drain_max_retries` | int | `3` | 上传失败最大重试次数 |
| `tree_refresh_ttl` | float | `10.0` | 目录树刷新间隔 (秒) |
| `list_page_size` | int | `100` | Drive Kit 列表接口每页条数 (1-100) |
| `http_timeout` | int | `30` | HTTP 请求超时 (秒) |
| `log_level` | string | `"INFO"` | 日志级别 |

### cloud_folder 三种模式

| 值 | 行为 | 示例 |
|------|------|------|
| `"applicationData"` | 挂载容器根目录（所有文件） | 默认值 |
| 文件夹名称 | 启动时在根目录查找同名文件夹，挂载其内容 | `"workspace"` |
| 文件夹 ID | 直接使用，无需查找 | `"Bom3iAdhu2F_7LBx..."` |

**典型部署场景:** 设置 `cloud_folder: "workspace"` 将容器路径 `/home/sandbox/.openclaw/workspace` 映射到云端 `workspace/` 文件夹，容器销毁后文件保留在云端。

## 性能

基于真实 Drive Kit API 测量（2026-04-26，中国大陆网络，1110 目录 + 500 文件）：

| 操作 | 延迟 | 说明 |
|------|------|------|
| `getattr` / `readdir` | **< 0.02ms** | 纯内存，无 API 调用 |
| `read`（缓存命中） | **~0.2ms** | 磁盘缓存读取 |
| `read`（缓存未命中） | **0.8-1.5s** | Drive Kit 下载 + 缓存填充 |
| `write` | **< 100ms** | 内存缓冲，异步上传 |
| `create` / `mkdir` | **0.6-1.0s** | Drive Kit API 调用 |
| 挂载启动 | **< 0.01s** | 仅初始化组件，后台线程异步加载元数据 |
| 后台全部加载 (1110 目录) | **~20s** | 8 线程并行 BFS |
| 首次访问未加载目录 | **~0.8s/级** | ensure_loaded 逐级加载用户请求路径 |

**Drive Kit API 注意事项:**

- 单次 API 调用约 800ms 固定开销，与文件大小无关
- 列表接口使用 `queryParam='{folderId}' in parentFolder` 过滤（Google Drive 风格语法）
- 每页最多 100 条（API 限制）
- 缓存命中后读取加速 **3000-4000 倍**
- 并行 BFS 加载比串行 BFS 快 **5 倍以上**

## 项目结构

```
clawfuse/
  __init__.py          # 包初始化
  cache.py             # LRU 磁盘缓存 (ContentCache)
  client.py            # Drive Kit REST API 客户端 (queryParam 过滤)
  config.py            # 配置数据类 (from_env / from_file)
  dirtree.py           # 内存目录树，懒加载 + 后台并行预加载
  exceptions.py        # DriveKitError, TokenError, ConfigError, MountError
  fuse.py              # FUSE 操作绑定 (ClawFUSE)
  lifecycle.py         # 生命周期管理：启动时解析文件夹 + 后台加载
  mount.py             # CLI 入口 (clawfuse --config)
  token.py             # 令牌管理器 (文件模式 / 字符串模式)
  writebuf.py          # 写缓冲 + 异步回传
tests/
  conftest.py          # 共享测试夹具
  test_cache.py        # 缓存测试
  test_client.py       # API 客户端测试
  test_config.py       # 配置测试
  test_dirtree.py      # 目录树测试
  test_fuse.py         # FUSE 操作测试
  test_lazy_load.py    # 懒加载 + 并发安全测试 (18 个)
  test_lifecycle.py    # 生命周期测试
  test_token.py        # 令牌管理测试
  test_writebuf.py     # 写缓冲测试
  test_perf.py         # 性能基准测试
  test_real_perf.py    # 真实 API 测试 (pytest.mark.realapi)
scripts/
  bench_metadata.py    # 元数据加载基准测试 (5^3 目录)
  bench_large.py       # 大规模压力测试 (10^3 目录)
pyproject.toml         # 项目配置 (hatchling)
clawfuse.json.example  # 配置文件模板
```

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试 (165 个，跳过真实 API)
pytest tests/ -k "not realapi"

# 代码检查
ruff check clawfuse/ tests/

# 类型检查
mypy clawfuse/
```

## 关键约束

- **令牌只读。** TokenManager 不会写入令牌文件，令牌刷新由外部完成。
- **每个文件单写入者。** 同一文件的并发写入会抛出 `WriteBufferError`。
- **Drive Kit API 要求 `containers=applicationData`。** 自动附加。
- **`applicationData` 是容器名不是文件夹 ID。** 启动时自动发现真实根目录 ID。
- **配置不可变。** `Config` 是 `@dataclass(frozen=True)`。

## 许可证

MIT
