# ClawFUSE

将华为 Drive Kit 云存储挂载为本地 FUSE 文件系统，服务于 OpenClaw 容器。

## 架构决策

**Drive Kit 是唯一的数据源。** 所有元数据在启动时通过 BFS 全量加载，文件内容按需下载，写入先缓冲到本地再异步上传。本地状态不作为权威数据。

## 模块列表

| 模块 | 职责 |
|------|------|
| `config.py` | 冻结数据类配置。`from_env()` 读取环境变量（传统模式），`from_file()` 读取 JSON 配置文件。构造时验证。 |
| `token.py` | 双模式令牌：文件模式（传统，60 秒重读缓存）或字符串模式（来自 JSON 配置，不可变）。 |
| `client.py` | Drive Kit REST API 封装。所有请求自动附带 `containers=applicationData`。处理 multipart 上传。 |
| `dirtree.py` | 内存目录树，从 Drive Kit 文件列表构建。通过父文件夹 ID 链做路径解析。 |
| `cache.py` | LRU 磁盘缓存。文件存储为 `{sha256}.data` + `.meta` JSON 附带文件。重启后可恢复。 |
| `writebuf.py` | 写缓冲 + 后台回传。文件以 `.buf` + `.meta` 入队，由 drain 线程上传。 |
| `fuse.py` | FUSE 操作绑定。将文件系统调用路由到 DirTree/Cache/WriteBuffer。每个文件单写入者。 |
| `lifecycle.py` | 生命周期管理：pre-start（令牌 + 文件夹解析 + DirTree 加载）和 pre-destroy（刷写待上传文件）。 |
| `mount.py` | CLI 入口。`--config` 指定 JSON 配置文件，否则回退到环境变量。信号处理器实现优雅关闭。 |
| `exceptions.py` | `DriveKitError`、`TokenError`、`ConfigError`、`MountError`。 |

## 开发命令

```bash
pip install -e ".[dev]"          # 安装开发依赖
pytest tests/                    # 运行全部测试
pytest tests/ -k "not realapi"   # 跳过真实 API 测试
ruff check clawfuse/ tests/      # 代码检查
mypy clawfuse/                   # 类型检查
```

## 关键约束

- **文件模式下令牌只读。** TokenManager 不会写入令牌文件。令牌刷新必须由外部完成。
- **每个文件单写入者。** WriteBuffer 对每个文件 ID 强制只有一个活跃写入者。同一文件的并发写入会抛出 `WriteBufferError`。
- **Drive Kit API 要求 `containers=applicationData`。** `DriveKitClient` 的 `_params()` 方法自动附加此参数。
- **Multipart 上传格式。** 创建和更新使用 Drive Kit multipart/form-data（元数据 JSON + 二进制内容）。
- **文件夹 ID vs 文件夹名称。** 启动时解析云端文件夹：`"applicationData"` 直接使用，短名称通过 `list_files` 查找，长字符串（20+ 字符）视为文件夹 ID。
- **配置不可变。** `Config` 是 `@dataclass(frozen=True)`。修改时需创建新的 Config 实例。
- **所有文件 UTF-8 编码。** 令牌文件、配置文件、源代码均使用 UTF-8。

## 测试

共 142 个测试（约 83% 覆盖率）。单元测试使用 mock DriveKitClient，不调用真实 API。真实 API 测试在 `tests/test_real_perf.py` 中，标记为 `pytest.mark.realapi`，需要有效令牌。
