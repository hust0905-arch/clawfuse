# ClawFUSE

将华为 Drive Kit 云存储挂载为本地 FUSE 文件系统，服务于 OpenClaw 容器。

## 架构决策

**Drive Kit 是唯一的数据源。** 元数据采用懒加载 + 后台并行 BFS 预加载：启动时仅加载根目录，后台线程并行加载剩余目录；用户请求未加载的目录时优先处理。文件内容按需下载，写入先缓冲到本地再异步上传。本地状态不作为权威数据。

## 模块列表

| 模块 | 职责 |
|------|------|
| `config.py` | 冻结数据类配置。`from_env()` 读取环境变量（传统模式），`from_file()` 读取 JSON 配置文件。构造时验证。 |
| `token.py` | 双模式令牌：文件模式（传统，60 秒重读缓存）或字符串模式（来自 JSON 配置，不可变）。 |
| `client.py` | Drive Kit REST API 封装。列表接口使用 `queryParam='{id}' in parentFolder` 过滤（Google Drive 风格语法）。所有请求自动附带 `containers=applicationData`。 |
| `dirtree.py` | 内存目录树，支持懒加载和后台并行预加载。`load_dir()` 按需加载单个目录，`ensure_loaded()` 按路径逐级加载，`background_full_load()` 用 ThreadPoolExecutor(8) 后台 BFS 并行加载全部目录。线程安全。 |
| `cache.py` | LRU 磁盘缓存。文件存储为 `{sha256}.data` + `.meta` JSON 附带文件。重启后可恢复。 |
| `writebuf.py` | 写缓冲 + 后台回传。文件以 `.buf` + `.meta` 入队，由 drain 线程上传。 |
| `fuse.py` | FUSE 操作绑定。`getattr`/`readdir` 先调用 `ensure_loaded` 确保元数据已加载，再查询 DirTree。写操作使用 `bytearray` 避免大文件 O(n^2) 拷贝。每个文件单写入者。 |
| `lifecycle.py` | 生命周期管理：pre-start（令牌 + 文件夹解析 + 启动后台元数据加载线程，不阻塞挂载）和 pre-destroy（刷写待上传文件）。`applicationData` 特殊处理：启动时发现真实根目录 ID。 |
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
- **列表接口使用 queryParam 过滤。** 格式为 `'{folderId}' in parentFolder`。直接传 `parentFolder` 参数会被 API 忽略。
- **`applicationData` 是容器名不是文件夹 ID。** 启动时通过 `_discover_application_data_root()` 发现真实根目录 ID。文件夹名称（如 `"workspace"`）通过在根目录列表中查找同名文件夹来解析。
- **Multipart 上传格式。** 创建和更新使用 Drive Kit multipart/form-data（元数据 JSON + 二进制内容）。
- **配置不可变。** `Config` 是 `@dataclass(frozen=True)`。修改时需创建新的 Config 实例。
- **所有文件 UTF-8 编码。** 令牌文件、配置文件、源代码均使用 UTF-8。
- **挂载子文件夹。** 设置 `cloud_folder: "workspace"` 可将 Drive Kit 根目录下的 `workspace` 子文件夹挂载到本地，其余文件（如打包备份）不会出现在 FUSE 中。
- **pageSize 上限 100。** Drive Kit API 验证 pageSize 在 1-100 范围内。

## 测试

共 165 个测试。单元测试使用 mock DriveKitClient，不调用真实 API。真实 API 测试在 `tests/test_real_perf.py` 中，标记为 `pytest.mark.realapi`，需要有效令牌。懒加载相关测试在 `tests/test_lazy_load.py`（18 个），覆盖 load_dir、ensure_loaded、background_full_load、并发安全及 FUSE 集成。
