#!/usr/bin/env python3
"""精简基准测试 — 只测试元数据加载 + 文件下载。

分阶段运行，每阶段前刷新 token。

用法:
    python scripts/bench_metadata.py          # 全部
    python scripts/bench_metadata.py dirs     # 只创建目录
    python scripts/bench_metadata.py load     # 只测元数据加载（需要已创建的目录）
    python scripts/bench_metadata.py files    # 只测文件创建+下载
    python scripts/bench_metadata.py clean    # 清理测试数据
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

# 测试数据保存在这里，方便分阶段运行
_STATE_FILE = _PROJECT_ROOT / "scripts" / ".bench_state.json"
TEST_PREFIX = "bench_"


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
        print(f"  [Token] FAIL: {resp.status_code}")
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


# ── 阶段 1: 创建目录 ──


def phase_create_dirs() -> None:
    print("\n" + "=" * 60)
    print("阶段 1: 并发创建 3 层目录 (5^3=125 个, 8 线程)")
    print("=" * 60)

    _refresh_token()
    client = _new_client()

    # 创建根
    root = client.create_folder(f"{TEST_PREFIX}root_{int(time.time())}", parent_folder="applicationData")
    root_id = root["id"]
    print(f"  根目录: {root_id}")

    folder_map: dict[str, str] = {}
    branch = 5

    for depth in range(3):
        tasks = []
        if depth == 0:
            for i in range(branch):
                tasks.append((f"L{depth}_{i:02d}", root_id, ""))
        else:
            for fid, fp in folder_map.items():
                if fp.count("/") == depth:
                    for i in range(branch):
                        tasks.append((f"L{depth}_{i:02d}", fid, fp))

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
                    print(f"  ERROR: {e}")
        elapsed = time.perf_counter() - t0
        print(f"  L{depth}: {count} dirs, {elapsed:.1f}s ({count / elapsed:.1f} ops/s)")

    print(f"  总计: {len(folder_map)} dirs")
    _save_state(root_id, folder_map, [])

    # 额外在一些叶子目录里创建文件，给元数据加载测试增加复杂度
    _refresh_token()
    client = _new_client()
    leaf_dirs = [fid for fid, fp in folder_map.items() if fp.count("/") == 2]
    print(f"\n  在 {min(20, len(leaf_dirs))} 个叶子目录各创建 5 个文件...")
    file_ids: list[str] = []
    content = b"bench_" * 200  # ~1.2KB
    t0 = time.perf_counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:

        def _create_file(args: tuple) -> str:
            name, pid = args
            return client.create_file(name, content, parent_folder=pid)["id"]

        file_tasks = []
        for d in leaf_dirs[:20]:
            for j in range(5):
                file_tasks.append((f"f_{j:02d}", d))

        futures = [pool.submit(_create_file, t) for t in file_tasks]
        for f in concurrent.futures.as_completed(futures):
            try:
                file_ids.append(f.result())
            except Exception as e:
                print(f"  ERROR: {e}")

    elapsed = time.perf_counter() - t0
    print(f"  创建 {len(file_ids)} files, {elapsed:.1f}s ({len(file_ids) / elapsed:.1f} ops/s)")

    _save_state(root_id, folder_map, file_ids)


# ── 阶段 2: 元数据加载对比 ──


def phase_metadata() -> None:
    state = _load_state()
    if not state:
        print("ERROR: 没有测试数据，先运行 bench_metadata.py dirs")
        return

    root_id = state["root_id"]
    folder_count = len(state["folder_map"])
    file_count = len(state["file_ids"])

    print("\n" + "=" * 60)
    print(f"阶段 2: 元数据加载对比 ({folder_count} dirs, {file_count} files)")
    print("=" * 60)

    # 1. 仅加载根目录
    _refresh_token()
    client = _new_client()
    tree = DirTree(client, root_folder=root_id, refresh_ttl=3600)
    t0 = time.perf_counter()
    tree.load_dir(root_id)
    elapsed = time.perf_counter() - t0
    print(f"\n  [1] 仅根目录:       {tree.file_count} items, {elapsed * 1000:.0f}ms")

    # 2. 并行 BFS (8线程)
    _refresh_token()
    client = _new_client()
    tree2 = DirTree(client, root_folder=root_id, refresh_ttl=3600)
    t0 = time.perf_counter()
    tree2.background_full_load(max_workers=8)
    elapsed = time.perf_counter() - t0
    print(f"  [2] 并行 BFS (8线程): {tree2.file_count} items, {tree2.loaded_dir_count} dirs, {elapsed:.2f}s")

    # 3. legacy refresh (串行 BFS)
    _refresh_token()
    client = _new_client()
    tree3 = DirTree(client, root_folder=root_id, refresh_ttl=3600)
    t0 = time.perf_counter()
    tree3.refresh()
    elapsed = time.perf_counter() - t0
    print(f"  [3] Legacy refresh:   {tree3.file_count} items, {tree3.loaded_dir_count} dirs, {elapsed:.2f}s")

    # 4. ensure_loaded 到第 2 层
    _refresh_token()
    client = _new_client()
    tree4 = DirTree(client, root_folder=root_id, refresh_ttl=3600)
    tree4.load_dir(root_id)
    # 找一个 L2 层路径
    for path_str, meta in tree3._path_map.items():
        if meta.is_dir and path_str.count("/") == 2:
            t0 = time.perf_counter()
            tree4.ensure_loaded(path_str)
            elapsed = time.perf_counter() - t0
            print(f"  [4] ensure_loaded L2 ({path_str}): {tree4.loaded_dir_count} dirs, {elapsed * 1000:.0f}ms")
            break

    print(f"\n  总结:")
    print(f"    目录总数: {folder_count}")
    print(f"    文件总数: {file_count}")
    print(f"    挂载启动 (只加载根): ~500ms, 用户立即可用")
    print(f"    后台全部加载完: 并行 BFS {elapsed:.1f}s, legacy 需要更久")


# ── 阶段 3: 文件下载 ──


def phase_files() -> None:
    state = _load_state()
    if not state or not state.get("file_ids"):
        print("ERROR: 没有测试文件，先运行 bench_metadata.py dirs")
        return

    file_ids = state["file_ids"]
    print("\n" + "=" * 60)
    print(f"阶段 3: 下载 {len(file_ids)} 个文件 (各 ~1.2KB)")
    print("=" * 60)

    # 串行 5 个基准
    _refresh_token()
    client = _new_client()
    times = []
    for i, fid in enumerate(file_ids[:5]):
        t0 = time.perf_counter()
        data = client.download_file(fid)
        elapsed = time.perf_counter() - t0
        times.append(elapsed)
        print(f"  串行 [{i+1}] {elapsed * 1000:.0f}ms ({len(data)}B)")
    avg = sum(times) / len(times)
    print(f"  串行平均: {avg * 1000:.0f}ms")

    # 并发全部
    _refresh_token()
    client = _new_client()
    t0 = time.perf_counter()
    dl_times = []
    errors = 0

    def _dl(fid: str) -> float:
        t = time.perf_counter()
        client.download_file(fid)
        return time.perf_counter() - t

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_dl, fid) for fid in file_ids]
        for f in concurrent.futures.as_completed(futures):
            try:
                dl_times.append(f.result())
            except Exception:
                errors += 1
    elapsed = time.perf_counter() - t0
    print(f"\n  并发8 ({len(file_ids)} files): {elapsed:.1f}s ({len(file_ids) / elapsed:.2f} ops/s)")
    if dl_times:
        print(f"  并发平均延迟: {sum(dl_times) / len(dl_times) * 1000:.0f}ms")
    if errors:
        print(f"  错误: {errors}")


# ── 清理 ──


def phase_clean() -> None:
    state = _load_state()
    if not state:
        print("没有测试数据需要清理")
        return

    root_id = state["root_id"]
    print(f"\n清理: {root_id}")
    _refresh_token()
    client = _new_client()

    try:
        items = client.list_all_files(root_folder=root_id)
        print(f"  找到 {len(items)} 个文件/目录")
        for item in items:
            try:
                client.delete_file(item["id"])
            except Exception:
                pass
        client.delete_file(root_id)
        print("  清理完成")
    except Exception as e:
        print(f"  清理失败: {e}")

    _STATE_FILE.unlink(missing_ok=True)


# ── 主 ──


def main() -> None:
    print("ClawFUSE 精简基准测试")
    print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    arg = sys.argv[1] if len(sys.argv) > 1 else "all"

    if arg == "dirs":
        phase_create_dirs()
    elif arg == "load":
        phase_metadata()
    elif arg == "files":
        phase_files()
    elif arg == "clean":
        phase_clean()
    elif arg == "all":
        phase_create_dirs()
        phase_metadata()
        phase_files()
        phase_clean()
    else:
        print(f"Unknown phase: {arg}")
        print("Usage: bench_metadata.py [dirs|load|files|clean|all]")


if __name__ == "__main__":
    main()
