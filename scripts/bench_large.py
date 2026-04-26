#!/usr/bin/env python3
"""大规模压力测试 — 10 分支 × 3 层 = 1000 目录 + 1000 文件。

之前 parentFolder 参数被 Drive Kit 忽略，导致每次 list_files 返回全量数据，
list_all_files BFS 卡死。现在 queryParam 修复后重跑，验证能否完成。

用法:
    python scripts/bench_large.py setup    # 创建测试数据
    python scripts/bench_large.py bench    # 跑基准测试
    python scripts/bench_large.py clean    # 清理
    python scripts/bench_large.py all      # 全部
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests as _req

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from clawfuse.client import DriveKitClient
from clawfuse.dirtree import DirTree
from clawfuse.token import TokenManager

_TOKEN_FILE = Path("D:/AI/drive_token.json")
if not _TOKEN_FILE.is_file():
    _TOKEN_FILE = _PROJECT_ROOT / "test_token.json"

_STATE_FILE = _PROJECT_ROOT / "scripts" / ".bench_large_state.json"
TEST_PREFIX = "bench_large_"

# 树形参数: branch^depth 个目录
BRANCH = 10   # 每个目录的子目录数
DEPTH = 3     # 目录层数 (不含根)
# 总目录数: 10 + 100 + 1000 = 1110

FILES_PER_LEAF = 10   # 每个叶子目录的文件数
LEAVES_TO_POPULATE = 50  # 在多少个叶子目录里创建文件


def _refresh_token() -> None:
    with open(_TOKEN_FILE) as f:
        data = json.load(f)
    resp = _req.post(
        "https://oauth-login.cloud.huawei.com/oauth2/v3/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"],
            "client_id": data["client_id"],
            "client_secret": data["client_secret"],
        },
        timeout=15,
    )
    if resp.status_code == 200:
        new = resp.json()
        data["access_token"] = new["access_token"]
        if "refresh_token" in new:
            data["refresh_token"] = new["refresh_token"]
        data["expires_in"] = new.get("expires_in", 3600)
        data["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(_TOKEN_FILE.parent), suffix=".tmp")
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(_TOKEN_FILE))
        print(f"  [Token] OK (expires_in={data['expires_in']}s)")
    else:
        print(f"  [Token] FAIL: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)


def _new_client() -> DriveKitClient:
    return DriveKitClient(TokenManager(_TOKEN_FILE.resolve()), timeout=60)


def _save_state(root_id: str, folder_map: dict, file_ids: list) -> None:
    with open(_STATE_FILE, "w") as f:
        json.dump({"root_id": root_id, "folder_map": folder_map, "file_ids": file_ids}, f)


def _load_state() -> dict:
    if not _STATE_FILE.is_file():
        return {}
    with open(_STATE_FILE) as f:
        return json.load(f)


# ── 阶段 1: 创建目录 + 文件 ──


def phase_setup() -> None:
    total_dirs = sum(BRANCH**d for d in range(1, DEPTH + 1))
    leaf_dirs_count = BRANCH**DEPTH
    total_files = LEAVES_TO_POPULATE * FILES_PER_LEAF

    print("\n" + "=" * 70)
    print(f"阶段 1: 创建测试数据")
    print(f"  目录结构: {BRANCH}^{DEPTH} = {total_dirs} 个目录 ({leaf_dirs_count} 个叶子)")
    print(f"  文件: {LEAVES_TO_POPULATE} 叶子 × {FILES_PER_LEAF} = {total_files} 个文件")
    print("=" * 70)

    _refresh_token()
    client = _new_client()

    # 创建根
    root = client.create_folder(f"{TEST_PREFIX}root_{int(time.time())}", parent_folder="applicationData")
    root_id = root["id"]
    print(f"\n  根目录: {root_id}")

    folder_map: dict[str, str] = {}
    errors: list[str] = []

    # 逐层创建
    for depth in range(DEPTH):
        tasks = []
        if depth == 0:
            for i in range(BRANCH):
                tasks.append((f"L{depth}_{i:03d}", root_id, ""))
        else:
            for fid, fp in folder_map.items():
                if fp.count("/") == depth:
                    for i in range(BRANCH):
                        tasks.append((f"L{depth}_{i:03d}", fid, fp))

        t0 = time.perf_counter()
        count = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:

            def _create(name: str, pid: str) -> str:
                return client.create_folder(name, parent_folder=pid)["id"]

            futures = {pool.submit(_create, name, pid): (name, fp) for name, pid, fp in tasks}
            for future in concurrent.futures.as_completed(futures):
                name, fp = futures[future]
                try:
                    fid = future.result()
                    folder_map[fid] = f"{fp}/{name}"
                    count += 1
                except Exception as e:
                    errors.append(f"{name}: {e}")

        elapsed = time.perf_counter() - t0
        print(f"  L{depth}: {count} dirs in {elapsed:.1f}s ({count / elapsed:.1f} ops/s)")

    if errors:
        print(f"  Warnings: {len(errors)} errors (first 3): {errors[:3]}")

    total_created = len(folder_map)
    print(f"\n  总目录: {total_created}")

    # Token 可能在长时间创建后过期
    print(f"\n  刷新 Token...")
    _refresh_token()
    client = _new_client()

    # 在叶子目录创建文件
    leaf_dirs = [fid for fid, fp in folder_map.items() if fp.count("/") == DEPTH - 1]
    populate_count = min(LEAVES_TO_POPULATE, len(leaf_dirs))
    print(f"  在 {populate_count} 个叶子目录各创建 {FILES_PER_LEAF} 个文件...")

    content = b"bench_large_" * 200  # ~2.4KB
    file_ids: list[str] = []
    t0 = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:

        def _create_file(args: tuple) -> str:
            name, pid = args
            return client.create_file(name, content, parent_folder=pid)["id"]

        file_tasks = []
        for d in leaf_dirs[:populate_count]:
            for j in range(FILES_PER_LEAF):
                file_tasks.append((f"f_{j:02d}", d))

        futures = [pool.submit(_create_file, t) for t in file_tasks]
        for f in concurrent.futures.as_completed(futures):
            try:
                file_ids.append(f.result())
            except Exception as e:
                errors.append(f"file: {e}")

    elapsed = time.perf_counter() - t0
    print(f"  创建 {len(file_ids)} files in {elapsed:.1f}s ({len(file_ids) / max(elapsed, 0.01):.1f} ops/s)")

    _save_state(root_id, folder_map, file_ids)
    print(f"\n  数据已保存到 {_STATE_FILE}")
    print(f"  目录: {total_created}, 文件: {len(file_ids)}")


# ── 阶段 2: 基准测试 ──


def phase_bench() -> None:
    state = _load_state()
    if not state:
        print("ERROR: 没有测试数据，先运行 bench_large.py setup")
        return

    root_id = state["root_id"]
    folder_count = len(state["folder_map"])
    file_count = len(state["file_ids"])
    total = folder_count + file_count

    print("\n" + "=" * 70)
    print(f"阶段 2: 元数据加载基准测试")
    print(f"  {folder_count} 个目录 + {file_count} 个文件 = {total} 个项目")
    print("=" * 70)

    # ── Test 1: load_dir(root) — 仅根目录 ──
    print(f"\n--- [1] load_dir(root): 仅加载根目录 ---")
    _refresh_token()
    client = _new_client()
    tree = DirTree(client, root_folder=root_id, refresh_ttl=3600)
    t0 = time.perf_counter()
    tree.load_dir(root_id)
    elapsed = time.perf_counter() - t0
    root_children = tree.list_dir("/")
    print(f"  耗时: {elapsed*1000:.0f}ms")
    print(f"  根目录子项: {len(root_children)} 个 {root_children}")
    assert len(root_children) == BRANCH, f"Expected {BRANCH} L0 dirs, got {len(root_children)}"

    # ── Test 2: ensure_loaded 到第 3 层 ──
    # 找一个 L2 叶子路径
    l2_paths = [fp for fp in state["folder_map"].values() if fp.count("/") == 3]
    if l2_paths:
        target = l2_paths[0]
        print(f"\n--- [2] ensure_loaded('{target}'): 逐层加载到第 {DEPTH} 层 ---")
        _refresh_token()
        client = _new_client()
        tree2 = DirTree(client, root_folder=root_id, refresh_ttl=3600)
        t0 = time.perf_counter()
        tree2.ensure_loaded(target)
        elapsed = time.perf_counter() - t0
        children = tree2.list_dir(target)
        print(f"  耗时: {elapsed*1000:.0f}ms")
        print(f"  加载目录数: {tree2.loaded_dir_count}")
        print(f"  目标子项: {len(children)} 个")
        assert tree2.loaded_dir_count == DEPTH + 1  # root + L0 + L1 + L2

    # ── Test 3: background_full_load (8 线程并行 BFS) ──
    print(f"\n--- [3] background_full_load(8): 并行 BFS 加载全部 {folder_count} 个目录 ---")
    _refresh_token()
    client = _new_client()
    tree3 = DirTree(client, root_folder=root_id, refresh_ttl=3600)
    t0 = time.perf_counter()
    tree3.background_full_load(max_workers=8)
    elapsed = time.perf_counter() - t0
    print(f"  耗时: {elapsed:.2f}s")
    print(f"  加载目录数: {tree3.loaded_dir_count}")
    print(f"  总项目数: {tree3.file_count}")
    assert tree3.bg_complete is True
    assert tree3.loaded_dir_count == folder_count + 1  # +root

    # ── Test 4: legacy refresh (list_all_files 串行 BFS) ──
    print(f"\n--- [4] legacy refresh: list_all_files 串行 BFS ---")
    _refresh_token()
    client = _new_client()
    tree4 = DirTree(client, root_folder=root_id, refresh_ttl=3600)
    t0 = time.perf_counter()
    tree4.refresh()
    elapsed = time.perf_counter() - t0
    print(f"  耗时: {elapsed:.2f}s")
    print(f"  总项目数: {tree4.file_count}")
    assert tree4.file_count == total

    # ── Summary ──
    print("\n" + "=" * 70)
    print("结果汇总:")
    print(f"  测试规模: {folder_count} 目录 + {file_count} 文件")
    print(f"  [1] load_dir(root):          仅根目录，毫秒级")
    print(f"  [2] ensure_loaded(3层深):     仅加载路径上的目录")
    print(f"  [3] background_full_load(8):  并行 BFS 全部加载")
    print(f"  [4] legacy refresh:           串行 BFS 全部加载")
    print("=" * 70)


# ── 清理 ──


def phase_clean() -> None:
    state = _load_state()
    if not state:
        print("没有测试数据需要清理")
        return

    root_id = state["root_id"]
    folder_count = len(state["folder_map"])
    file_count = len(state["file_ids"])
    print(f"\n清理: {root_id} ({folder_count} dirs, {file_count} files)")

    _refresh_token()
    client = _new_client()

    # 先用 background_full_load 拿到所有 ID（比 list_all_files 快）
    tree = DirTree(client, root_folder=root_id, refresh_ttl=3600)
    t0 = time.perf_counter()
    tree.background_full_load(max_workers=8)
    elapsed = time.perf_counter() - t0
    print(f"  枚举完成: {tree.file_count} items in {elapsed:.1f}s")

    # 并发删除
    all_ids = list(tree._id_map.keys()) + [root_id]
    deleted = 0
    errors_del = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(client.delete_file, fid) for fid in all_ids]
        for f in concurrent.futures.as_completed(futures):
            try:
                f.result()
                deleted += 1
            except Exception:
                errors_del += 1

    print(f"  删除: {deleted} 成功, {errors_del} 失败")
    _STATE_FILE.unlink(missing_ok=True)
    print("  清理完成")


# ── 主 ──


def main() -> None:
    print("ClawFUSE 大规模压力测试 (queryParam 修复版)")
    print(f"参数: branch={BRANCH}, depth={DEPTH}")
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    arg = sys.argv[1] if len(sys.argv) > 1 else "all"

    if arg == "setup":
        phase_setup()
    elif arg == "bench":
        phase_bench()
    elif arg == "clean":
        phase_clean()
    elif arg == "all":
        phase_setup()
        phase_bench()
        phase_clean()
    else:
        print(f"Usage: bench_large.py [setup|bench|clean|all]")


if __name__ == "__main__":
    main()
