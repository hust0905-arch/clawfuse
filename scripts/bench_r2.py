"""Round 2 benchmark: Extreme scenario (1000 files, 100 dirs, 5 levels + large files)."""
import os
import time
import statistics
import threading
import subprocess

MP = "/home/sandbox/.openclaw/workspace"
PREFIX = "r2"  # prefix to namespace test data


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, timeout=120)


def time_op(fn, n=1):
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times) if n > 1 else times[0]


def setup_data():
    """Create 1000 files in 100 dirs, 5 levels deep + large files."""
    print("Setting up extreme test data...")
    t0 = time.time()

    # 5-level structure: r2/L0_{0-1}/L1_{0-1}/L2_{0-1}/L3_{0-1}/L4_{0-4}
    dirs_created = 0
    base = f"{MP}/{PREFIX}"

    # Level 0-4: 2 x 2 x 2 x 2 x 5 = 80 leaf dirs, ~120 total dirs
    for l0 in range(2):
        for l1 in range(2):
            for l2 in range(2):
                for l3 in range(2):
                    for l4 in range(5):
                        d = f"{base}/L0_{l0}/L1_{l1}/L2_{l2}/L3_{l3}/L4_{l4}"
                        os.makedirs(d, exist_ok=True)
                        dirs_created += 1

    # Create 1000 files: ~10 per leaf dir, 1200 bytes each
    files_created = 0
    for l0 in range(2):
        for l1 in range(2):
            for l2 in range(2):
                for l3 in range(2):
                    for l4 in range(5):
                        d = f"{base}/L0_{l0}/L1_{l1}/L2_{l2}/L3_{l3}/L4_{l4}"
                        for fi in range(12):
                            fp = os.path.join(d, f"f_{fi:02d}")
                            with open(fp, "wb") as f:
                                f.write(os.urandom(1200))
                            files_created += 1
                            if files_created >= 1000:
                                break
                        if files_created >= 1000:
                            break
                    if files_created >= 1000:
                        break
                if files_created >= 1000:
                    break
            if files_created >= 1000:
                break
        if files_created >= 1000:
            break

    # Create large files at root level
    for size, label in [(1024 * 1024, "1MB"), (5 * 1024 * 1024, "5MB")]:
        for i in range(3):
            fp = f"{base}/large_{label}_{i}.bin"
            with open(fp, "wb") as f:
                f.write(os.urandom(size))

    elapsed = time.time() - t0
    print(f"  Created {files_created} files + 6 large files, {dirs_created} leaf dirs in {elapsed:.1f}s")
    return files_created, dirs_created


def cleanup_data():
    """Remove all test data."""
    import shutil
    base = f"{MP}/{PREFIX}"
    if os.path.exists(base):
        shutil.rmtree(base)
    print("  Cleaned up test data.")


def main():
    print("=" * 60)
    print("CLAWFUSE ROUND 2 BENCHMARK (EXTREME)")
    print("=" * 60)

    # --- Setup ---
    files_created, dirs_created = setup_data()

    # Wait for write buffer to flush
    print("  Waiting for writes to flush (10s)...")
    time.sleep(10)

    # --- Kill, clear cache, restart for cold start ---
    print()
    print("Restarting with fresh cache...")
    sh("pkill -f 'python.*mount.py'")
    time.sleep(2)
    sh("rm -rf /tmp/clawfuse-cache/* /tmp/clawfuse-writes/*")
    time.sleep(1)
    sh(
        "cd /root/clawfuse && nohup python -m clawfuse.mount "
        "--config /root/clawfuse/clawfuse.json "
        f"--mount-point {MP} --foreground "
        "> /tmp/clawfuse_r2.log 2>&1 & echo $! > /tmp/clawfuse.pid"
    )
    time.sleep(3)

    print()
    print("--- TEST 1: Cold access to 5-level deep file ---")
    deep_file = None
    for root, dirs, files in os.walk(f"{MP}/{PREFIX}"):
        depth = root.replace(MP, "").count(os.sep)
        if depth >= 5 and files:
            deep_file = os.path.join(root, files[0])
            break
    if deep_file:
        t = time_op(lambda: os.stat(deep_file))
        print(f"  Cold stat 5-level deep: {t:.0f}ms  ({deep_file})")

        t = time_op(lambda: open(deep_file, "rb").read())
        print(f"  Cold read 5-level deep (1.2KB): {t:.0f}ms")

    print()
    print("--- TEST 2: Large file reads ---")
    for size_label in ["1MB", "5MB"]:
        fp = f"{MP}/{PREFIX}/large_{size_label}_0.bin"
        if os.path.exists(fp):
            t = time_op(lambda p=fp: open(p, "rb").read())
            print(f"  Cold read {size_label}: {t:.0f}ms")

    print()
    print("--- TEST 3: Warm operations (post-preload) ---")
    # Wait for background BFS to finish
    time.sleep(5)

    if deep_file:
        t = time_op(lambda: os.stat(deep_file), n=50)
        print(f"  Warm stat (p50/50x): {t:.3f}ms")

        t = time_op(lambda p=deep_file: open(p, "rb").read(), n=20)
        print(f"  Warm read 1.2KB (p50/20x): {t:.2f}ms")

    for size_label in ["1MB", "5MB"]:
        fp = f"{MP}/{PREFIX}/large_{size_label}_0.bin"
        if os.path.exists(fp):
            t = time_op(lambda p=fp: open(p, "rb").read(), n=10)
            print(f"  Warm read {size_label} (p50/10x): {t:.1f}ms")

    print()
    print("--- TEST 4: Concurrent read (16 threads) ---")
    all_files = []
    for root, dirs, files in os.walk(f"{MP}/{PREFIX}"):
        for f in files:
            fp = os.path.join(root, f)
            if os.path.getsize(fp) <= 2000:  # small files only
                all_files.append(fp)

    read_results = []

    def do_read(fp):
        t0 = time.perf_counter()
        with open(fp, "rb") as fh:
            fh.read()
        read_results.append(time.perf_counter() - t0)

    # First pass (cold-ish, some may be cached from walk)
    threads = []
    test_files = all_files[:48]  # 48 ops
    for fp in test_files:
        t = threading.Thread(target=do_read, args=(fp,))
        threads.append(t)
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = time.perf_counter() - t_start
    qps = len(read_results) / total
    print(f"  16T concurrent read ({len(read_results)} files): {qps:.1f} QPS  total={total*1000:.0f}ms")

    # Warm pass
    read_results.clear()
    threads = []
    for fp in test_files:
        t = threading.Thread(target=do_read, args=(fp,))
        threads.append(t)
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = time.perf_counter() - t_start
    qps_warm = len(read_results) / total
    print(f"  16T warm read ({len(read_results)} files): {qps_warm:.1f} QPS  total={total*1000:.0f}ms")

    print()
    print("--- TEST 5: Concurrent readdir (16 threads) ---")
    all_dirs = []
    for root, dirs, _ in os.walk(f"{MP}/{PREFIX}"):
        all_dirs.append(root)

    ls_results = []

    def do_ls(d):
        t0 = time.perf_counter()
        os.listdir(d)
        ls_results.append(time.perf_counter() - t0)

    threads = []
    for d in all_dirs[:48]:
        t = threading.Thread(target=do_ls, args=(d,))
        threads.append(t)
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = time.perf_counter() - t_start
    qps = len(ls_results) / total
    print(f"  16T concurrent readdir ({len(ls_results)} dirs): {qps:.1f} QPS  total={total*1000:.0f}ms")

    print()
    print("--- TEST 6: Large file write ---")
    data_1mb = os.urandom(1024 * 1024)
    t = time_op(lambda: open(f"{MP}/{PREFIX}/bench_write_1mb.bin", "wb").write(data_1mb))
    print(f"  Write 1MB: {t:.0f}ms")

    data_5mb = os.urandom(5 * 1024 * 1024)
    t = time_op(lambda: open(f"{MP}/{PREFIX}/bench_write_5mb.bin", "wb").write(data_5mb))
    print(f"  Write 5MB: {t:.0f}ms")

    print()
    print("--- TEST 7: Batch create (100 files) ---")
    t0 = time.perf_counter()
    for i in range(100):
        with open(f"{MP}/{PREFIX}/bench_batch_{i:03d}.txt", "w") as f:
            f.write(f"batch file {i}")
    batch_create_ms = (time.perf_counter() - t0) * 1000
    print(f"  Create 100 files: {batch_create_ms:.0f}ms  ({100 / (batch_create_ms / 1000):.1f} ops/s)")

    time.sleep(8)  # Wait for flush

    print()
    print("--- TEST 8: Batch delete (100 files) ---")
    t0 = time.perf_counter()
    for i in range(100):
        os.unlink(f"{MP}/{PREFIX}/bench_batch_{i:03d}.txt")
    batch_delete_ms = (time.perf_counter() - t0) * 1000
    print(f"  Delete 100 files: {batch_delete_ms:.0f}ms  ({100 / (batch_delete_ms / 1000):.1f} ops/s)")

    print()
    print("--- TEST 9: Deep path creation (5 levels) ---")
    t0 = time.perf_counter()
    os.makedirs(f"{MP}/{PREFIX}/bench_deep/a/b/c/d/e", exist_ok=True)
    deep_create_ms = (time.perf_counter() - t0) * 1000
    print(f"  mkdir -p 5 levels: {deep_create_ms:.0f}ms")

    # Cleanup bench files
    for f in ["bench_write_1mb.bin", "bench_write_5mb.bin"]:
        fp = f"{MP}/{PREFIX}/{f}"
        if os.path.exists(fp):
            os.unlink(fp)
    import shutil
    if os.path.exists(f"{MP}/{PREFIX}/bench_deep"):
        shutil.rmtree(f"{MP}/{PREFIX}/bench_deep")

    print()
    print("--- Background preload log ---")
    with open("/tmp/clawfuse_r2.log") as f:
        for line in f:
            if "ready" in line or "Background full load" in line:
                print(f"  {line.strip()}")

    print()
    print("=" * 60)
    print("ROUND 2 COMPLETE")
    print("=" * 60)

    # Cleanup
    print()
    cleanup_data()


if __name__ == "__main__":
    main()
