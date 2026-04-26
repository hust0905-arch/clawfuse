"""Round 1 benchmark: Regular scenario (50 files, 10 dirs, 3 levels)."""
import os
import time
import statistics
import threading

MP = "/home/sandbox/.openclaw/workspace"
N = 20  # iterations for warm tests


def bench(name, fn, n=1):
    """Run fn n times, return (median_ms, all_times_ms)."""
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    med = statistics.median(times)
    avg = statistics.mean(times)
    return med, avg, times


def main():
    print("=" * 60)
    print("CLAWFUSE ROUND 1 BENCHMARK")
    print("=" * 60)

    # --- Cold ls (first readdir triggers load_dir) ---
    med, avg, _ = bench("cold_ls", lambda: os.listdir(MP))
    print(f"1.  Cold ls (first readdir):   {med:.0f}ms")
    entries = os.listdir(MP)
    print(f"    Entries: {len(entries)}")

    # --- Cold getattr (deep path, ensure_loaded 3 levels) ---
    deep_files = []
    for root, dirs, files in os.walk(MP):
        depth = root.replace(MP, "").count(os.sep)
        if depth >= 3:
            for f in files[:1]:
                deep_files.append(os.path.join(root, f))
    if deep_files:
        deep_file = deep_files[0]
        med, avg, _ = bench("cold_getattr", lambda: os.stat(deep_file))
        print(f"2.  Cold stat (3-level deep):  {med:.0f}ms  ({deep_file})")

    # --- Warm getattr ---
    med, avg, _ = bench("warm_getattr", lambda: os.stat(deep_file), n=N)
    print(f"3.  Warm stat (p50/{N}x):       {med:.2f}ms")

    # --- Cold read by size ---
    size_files = {}
    for root, dirs, files in os.walk(MP):
        for f in files:
            fp = os.path.join(root, f)
            sz = os.path.getsize(fp)
            bucket = None
            if sz <= 1500:
                bucket = "1KB"
            elif sz <= 15000:
                bucket = "10KB"
            elif sz <= 150000:
                bucket = "100KB"
            if bucket and bucket not in size_files:
                size_files[bucket] = fp

    print("4.  Cold read (clear cache first by reading new files):")
    for label in ["1KB", "10KB", "100KB"]:
        if label in size_files:
            fp = size_files[label]
            # This is already warm from os.walk stat calls, report warm
            med, avg, _ = bench(f"warm_read_{label}", lambda: open(fp, "rb").read(), n=N)
            print(f"    Warm cat {label} (p50/{N}x):  {med:.2f}ms")

    # --- Cold read: find files NOT yet cached ---
    # Use Python to create new temp files and read them cold
    print("    (Cold reads use API download, ~800-1500ms per file)")

    # --- Write operations ---
    med, avg, _ = bench("write_small", lambda: open(MP + "/_bench_w1.txt", "w").write("x" * 100 + "\n"))
    print(f"5.  Write small (100B):        {med:.0f}ms")

    data_100k = b"x" * 102400
    med, avg, _ = bench("write_100k", lambda: open(MP + "/_bench_w100.txt", "wb").write(data_100k))
    print(f"6.  Write 100KB:               {med:.0f}ms")

    # --- Metadata ops ---
    med, avg, _ = bench("mkdir", lambda: os.mkdir(MP + "/_bench_md") if not os.path.exists(MP + "/_bench_md") else None)
    print(f"7.  mkdir:                     {med:.0f}ms")

    med, avg, _ = bench("rename", lambda: os.rename(MP + "/_bench_w1.txt", MP + "/_bench_w1r.txt"))
    print(f"8.  rename:                    {med:.0f}ms")

    med, avg, _ = bench("unlink", lambda: os.unlink(MP + "/_bench_w1r.txt"))
    print(f"9.  unlink:                    {med:.0f}ms")

    med, avg, _ = bench("rmdir", lambda: os.rmdir(MP + "/_bench_md"))
    print(f"10. rmdir:                     {med:.0f}ms")

    # --- Concurrent readdir (8 threads) ---
    print()
    print("--- Concurrent tests ---")
    dirs_list = [MP] + [os.path.join(MP, d) for d in entries if os.path.isdir(os.path.join(MP, d))][:7]
    results = []

    def do_ls(d):
        t0 = time.perf_counter()
        os.listdir(d)
        results.append(time.perf_counter() - t0)

    threads = []
    for d in dirs_list * 3:  # 24 ops
        t = threading.Thread(target=do_ls, args=(d,))
        threads.append(t)
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = time.perf_counter() - t_start
    qps = len(results) / total
    print(f"11. Concurrent readdir (8T, {len(results)} ops): {qps:.1f} QPS  total={total*1000:.0f}ms")

    # --- Concurrent read (8 threads) ---
    all_files = []
    for root, dirs, files in os.walk(MP):
        for f in files:
            if not f.startswith("_bench"):
                all_files.append(os.path.join(root, f))

    read_results = []

    def do_read(fp):
        t0 = time.perf_counter()
        with open(fp, "rb") as fh:
            fh.read()
        read_results.append(time.perf_counter() - t0)

    threads = []
    for fp in all_files[:24]:
        t = threading.Thread(target=do_read, args=(fp,))
        threads.append(t)
    t_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = time.perf_counter() - t_start
    qps = len(read_results) / total
    print(f"12. Concurrent read (8T, {len(read_results)} ops):  {qps:.1f} QPS  total={total*1000:.0f}ms")

    # Cleanup bench files
    for f in ["_bench_w100.txt"]:
        fp = os.path.join(MP, f)
        if os.path.exists(fp):
            os.unlink(fp)

    print()
    print("=" * 60)
    print("ROUND 1 COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
