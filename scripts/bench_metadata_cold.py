"""Cold-start metadata loading benchmark v4.

Key improvements over v3:
- Each scenario runs independently (create → test → cleanup)
- True cold stat: access known path directly, no os.walk
- Clear log file on each restart
- Python -u for unbuffered output
"""
import os
import sys
import time
import subprocess
import statistics
from pathlib import Path

sys.path.insert(0, "/root/clawfuse")
from clawfuse.client import DriveKitClient
from clawfuse.config import Config
from clawfuse.token import TokenManager

MP = "/home/sandbox/.openclaw/workspace"


def new_client():
    cfg = Config.from_file(Path("/root/clawfuse/clawfuse.json"))
    tm = TokenManager(token_string=cfg.token_string)
    return DriveKitClient(tm, timeout=60)


def sh(cmd, timeout=30):
    return subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)


def refresh_token():
    """Refresh token and update config."""
    sh("python3 /tmp/refresh_token.py > /tmp/fresh_token.txt 2>&1")
    import json
    try:
        with open("/tmp/fresh_token.txt") as f:
            token = f.read().strip()
        if token and "Error" not in token and len(token) > 20:
            with open("/root/clawfuse/clawfuse.json") as f:
                cfg = json.load(f)
            cfg["token"] = token
            with open("/root/clawfuse/clawfuse.json", "w") as f:
                json.dump(cfg, f, indent=2)
            return True
    except Exception:
        pass
    return False


def restart_fuse(log_file):
    """Kill FUSE, clear cache, restart with fresh token."""
    refresh_token()
    sh("pkill -f 'python.*mount.py'")
    time.sleep(2)
    sh("umount -f " + MP + " 2>/dev/null")
    time.sleep(1)
    sh("rm -rf /tmp/clawfuse-cache/* /tmp/clawfuse-writes/*")
    time.sleep(1)
    # Clear log file
    open(log_file, "w").close()
    sh(
        "cd /root/clawfuse && nohup python3 -u -m clawfuse.mount "
        "--config /root/clawfuse/clawfuse.json "
        f"--mount-point {MP} --foreground "
        f"> {log_file} 2>&1 & echo $! > /tmp/clawfuse.pid"
    )
    # Wait for FUSE mount
    for _ in range(30):
        result = sh("mount | grep -q " + MP + " && echo MOUNTED || echo NO")
        if "MOUNTED" in result.stdout.decode():
            time.sleep(0.5)
            return True
        time.sleep(0.5)
    print("  WARNING: FUSE mount may have failed!")
    return False


def wait_for_bfs(log_file, max_wait=300):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < max_wait:
        try:
            with open(log_file) as f:
                for line in f:
                    if "Background full load complete" in line:
                        return True, line.strip(), time.perf_counter() - t0
        except FileNotFoundError:
            pass
        time.sleep(0.5)
    return False, "TIMEOUT", max_wait


def create_tree_api(client, workspace_id, prefix, branch, depth, files_per_leaf):
    """Create dirs + files via API directly under workspace_id."""
    dir_ids = {}
    file_count = 0

    def create_level(parent_id, path_parts, level):
        nonlocal file_count
        if level >= depth:
            for i in range(files_per_leaf):
                content = os.urandom(1200)
                client.create_file(
                    filename="f_{}".format(i),
                    content=content,
                    parent_folder=parent_id,
                )
                file_count += 1
            return

        for i in range(branch):
            name = "L{}_{}".format(level, i)
            result = client.create_folder(name, parent_id)
            folder_id = result.get("id", "")
            child_path = "/".join(path_parts + [name])
            dir_ids[child_path] = folder_id
            create_level(folder_id, path_parts + [name], level + 1)

    result = client.create_folder(prefix, workspace_id)
    prefix_id = result.get("id", "")
    dir_ids[prefix] = prefix_id

    create_level(prefix_id, [prefix], 0)
    return len(dir_ids), file_count


def cleanup_api(client, workspace_id, prefix):
    """Delete a prefix folder and all its contents."""
    res = client.list_files(workspace_id)
    for f in res.get("files", []):
        if f.get("fileName") == prefix:
            client.delete_file(f["id"])
            return True
    return False


def run_scenario(client, workspace_id, prefix, branch, depth, files_per_leaf, log_file):
    """Run one complete scenario: create → cold test → warm test → cleanup."""
    # Calculate expected counts
    total_dirs = sum(branch ** i for i in range(depth + 1))  # includes prefix dir
    leaf_dirs = branch ** depth
    total_files = leaf_dirs * files_per_leaf
    label = "{} dirs / {} files".format(total_dirs, total_files)

    print("\n--- {}: {} ---".format(prefix, label))

    # Phase A: Create test data
    print("  Creating test data via API...")
    t0 = time.time()
    n_dirs, n_files = create_tree_api(client, workspace_id, prefix, branch, depth, files_per_leaf)
    create_time = time.time() - t0
    print("  Created {} dirs, {} files in {:.1f}s".format(n_dirs, n_files, create_time))

    # Refresh token before restart (creation may take minutes)
    print("  Refreshing token...")
    refresh_token()

    # Phase B: Restart FUSE with fresh cache
    print("  Restarting FUSE...")
    if not restart_fuse(log_file):
        print("  FAILED to mount!")
        cleanup_api(client, workspace_id, prefix)
        return None

    # Phase C: TRUE cold access - directly stat a known deep path
    # Use the first branch at each level: prefix/L0_0/L1_0/.../f_00
    deep_path_parts = [MP, prefix]
    for level in range(depth):
        deep_path_parts.append("L{}_0".format(level))
    deep_path_parts.append("f_0")
    deep_path = "/".join(deep_path_parts)

    results = {"label": label, "prefix": prefix, "n_dirs": n_dirs, "n_files": n_files}

    # Cold stat (true cold - no os.walk, direct access)
    print("  Testing cold stat: {} ...".format(deep_path.replace(MP, "")))
    t0 = time.perf_counter()
    try:
        os.stat(deep_path)
        cold_stat_ms = (time.perf_counter() - t0) * 1000
    except Exception as e:
        cold_stat_ms = -1
        print("  Cold stat error: {}".format(e))
    results["cold_stat_ms"] = cold_stat_ms
    print("  Cold stat: {:.0f}ms".format(cold_stat_ms))

    # Cold read
    t0 = time.perf_counter()
    try:
        with open(deep_path, "rb") as f:
            f.read()
        cold_read_ms = (time.perf_counter() - t0) * 1000
    except Exception as e:
        cold_read_ms = -1
        print("  Cold read error: {}".format(e))
    results["cold_read_ms"] = cold_read_ms
    print("  Cold read: {:.0f}ms".format(cold_read_ms))

    # Cold ls root (first readdir after mount)
    t0 = time.perf_counter()
    try:
        os.listdir(MP)
    except Exception:
        pass
    cold_ls_ms = (time.perf_counter() - t0) * 1000
    results["cold_ls_ms"] = cold_ls_ms
    print("  Cold ls root: {:.0f}ms".format(cold_ls_ms))

    # BFS full load time
    print("  Waiting for background BFS...")
    ok, line, bfs_s = wait_for_bfs(log_file)
    results["bfs_s"] = bfs_s
    print("  BFS full load: {:.1f}s".format(bfs_s))

    # Warm stat after BFS
    times = []
    for _ in range(50):
        t0 = time.perf_counter()
        os.stat(deep_path)
        times.append((time.perf_counter() - t0) * 1000)
    warm_stat = statistics.median(times)
    results["warm_stat_ms"] = warm_stat
    print("  Warm stat (p50/50x): {:.3f}ms".format(warm_stat))

    # Warm read after BFS
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        with open(deep_path, "rb") as f:
            f.read()
        times.append((time.perf_counter() - t0) * 1000)
    warm_read = statistics.median(times)
    results["warm_read_ms"] = warm_read
    print("  Warm read (p50/20x): {:.2f}ms".format(warm_read))

    # Full ls -R time (how long to walk entire tree after BFS)
    t0 = time.perf_counter()
    file_list = []
    for root, dirs, files in os.walk(MP):
        for f in files:
            file_list.append(os.path.join(root, f))
    walk_s = time.perf_counter() - t0
    results["walk_s"] = walk_s
    results["walk_files"] = len(file_list)
    print("  ls -R entire tree: {:.1f}s ({} files)".format(walk_s, len(file_list)))

    # Phase D: Cleanup
    print("  Cleaning up...")
    cleanup_api(client, workspace_id, prefix)
    print("  Done.")

    return results


def main():
    print("=" * 70)
    print("COLD-START METADATA LOADING BENCHMARK v4")
    print("=" * 70)

    # Initial token refresh
    print("\nRefreshing token...")
    if not refresh_token():
        print("Token refresh failed!")
        return
    print("Token refreshed.")

    client = new_client()

    # Find workspace folder
    ws_result = client.list_files("applicationData")
    ws_id = None
    for f in ws_result.get("files", []):
        if f.get("fileName") == "workspace":
            ws_id = f["id"]
            break
    if not ws_id:
        print("ERROR: workspace folder not found!")
        return
    print("Workspace ID: {}".format(ws_id))

    # Scenarios: each runs independently
    scenarios = [
        ("s100", 4, 3, 3),     # 85 dirs, 192 files
        ("s500", 6, 3, 2),     # 259 dirs, 432 files
        ("s1000", 10, 3, 1),   # 1111 dirs, 1000 files
    ]

    all_results = {}
    log_file = "/tmp/clawfuse_cold.log"

    for prefix, branch, depth, files_per_leaf in scenarios:
        result = run_scenario(client, ws_id, prefix, branch, depth, files_per_leaf, log_file)
        if result:
            all_results[prefix] = result

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    header = "{:<20} {:>6} {:>6} {:>10} {:>10} {:>10} {:>8} {:>10} {:>10} {:>10}".format(
        "Scenario", "Dirs", "Files", "Cold ls", "Cold stat", "Cold read", "BFS(s)", "BFS+walk", "Warm stat", "Warm read"
    )
    print(header)
    print("-" * len(header))
    for k, v in all_results.items():
        print("{:<20} {:>6} {:>6} {:>9.0f}ms {:>9.0f}ms {:>9.0f}ms {:>7.1f}s {:>9.1f}s {:>9.3f}ms {:>9.2f}ms".format(
            v["label"],
            v["n_dirs"],
            v["n_files"],
            v.get("cold_ls_ms", 0),
            v["cold_stat_ms"],
            v["cold_read_ms"],
            v["bfs_s"],
            v.get("walk_s", 0),
            v["warm_stat_ms"],
            v["warm_read_ms"],
        ))
    print()
    print("Comparison: 20GB compressed download (1Gbps) + extract = ~227s")
    print("=" * 70)


if __name__ == "__main__":
    main()
