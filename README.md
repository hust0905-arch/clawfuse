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
| `list_page_size` | int | `200` | Drive Kit 列表接口每页条数 |
| `http_timeout` | int | `30` | HTTP 请求超时 (秒) |
| `log_level` | string | `"INFO"` | 日志级别 |

### 云端文件夹

- `"applicationData"` — 默认容器数据文件夹，直接使用，无需解析
- 文件夹名称（如 `"workspace"`）— 启动时通过 `list_files` 解析为 ID
- 文件夹 ID（20+ 字符的字符串）— 直接使用

## 性能

基于真实 Drive Kit API 测量（2026-04-24，中国大陆网络）：

| 操作 | 延迟 | 说明 |
|------|------|------|
| `getattr` / `readdir` | **< 0.02ms** | 纯内存，无 API 调用 |
| `read`（缓存命中） | **~0.2ms** | 磁盘缓存读取 |
| `read`（缓存未命中） | **0.8-1.5s** | Drive Kit 下载 + 缓存填充 |
| `write` | **< 100ms** | 内存缓冲，异步上传 |
| `create` / `mkdir` | **0.6-1.0s** | Drive Kit API 调用 |
| 挂载启动 | **< 0.1s** | 仅加载根目录，不阻塞；后台线程并行预加载全部元数据 |
| 首次访问未加载目录 | **~0.8s/级** | ensure_loaded 逐级加载用户请求路径，后台继续并行加载剩余 |

**核心发现:** Drive Kit 单次 API 调用有约 800ms 固定开销，与文件大小无关。缓存命中后读取加速 **3000-4000 倍**。挂载采用懒加载，启动不阻塞，后台 8 线程并行预加载元数据。

## 项目结构

```
clawfuse/
  __init__.py          # 包初始化
  cache.py             # LRU 磁盘缓存 (ContentCache)
  client.py            # Drive Kit REST API 客户端
  config.py            # 配置数据类 (from_env / from_file)
  dirtree.py           # 内存目录树 (DirTree)
  exceptions.py        # 自定义异常
  fuse.py              # FUSE 操作绑定 (ClawFUSE)
  lifecycle.py         # 生命周期管理 (pre-start / pre-destroy)
  mount.py             # CLI 入口
  token.py             # 令牌管理器 (文件模式 / 字符串模式)
  writebuf.py          # 写缓冲 + 异步回传
tests/
  conftest.py          # 共享测试夹具
  test_lazy_load.py    # 懒加载 + 并发安全测试
  test_*.py            # 单元测试 + 性能测试
docs/
  ClawFUSE-架构设计说明书.md
  ClawFUSE-详细设计说明书.md
  ClawFUSE-性能测试报告.md
```

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest tests/

# 跳过真实 API 测试
pytest tests/ -k "not realapi"

# 代码检查
ruff check clawfuse/ tests/

# 类型检查
mypy clawfuse/
```

## 许可证

MIT
