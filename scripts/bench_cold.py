"""Cold-start benchmark: clear cache, restart, measure first-access latency."""
import os
import time
import subprocess
import statistics

MP = "/home/sandbox/.openclaw/workspace"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, timeout=30)


def time_op(fn, n=1):
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times) if n > 1 else times[0]


def main():
    # Kill and clear cache
    sh("pkill -f 'python.*mount.py'")
    time.sleep(1)
    sh("rm -rf /tmp/clawfuse-cache/* /tmp/clawfuse-writes/*")
    time.sleep(1)

    # Start ClawFUSE
    sh(
        "cd /root/clawfuse && nohup python -m clawfuse.mount "
        "--config /root/clawfuse/clawfuse.json "
        f"--mount-point {MP} --foreground "
        "> /tmp/clawfuse_cold.log 2>&1 & echo $! > /tmp/clawfuse.pid"
    )
    time.sleep(3)

    print("=" * 60)
    print("COLD START BENCHMARK (fresh cache)")
    print("=" * 60)

    # 1. First ls (triggers load_dir root)
    t = time_op(lambda: os.listdir(MP))
    print(f"1.  First ls (cold readdir):   {t:.0f}ms")

    # 2. Cold stat deep file
    deep = None
    for root, dirs, files in os.walk(MP):
        depth = root.replace(MP, "").count(os.sep)
        if depth >= 3 and files:
            deep = os.path.join(root, files[0])
            break
    if deep:
        t = time_op(lambda: os.stat(deep))
        print(f"2.  Cold stat (3-level deep):  {t:.0f}ms")

    # 3. Cold read by size
    by_size = {}
    for root, dirs, files in os.walk(MP):
        for f in files:
            fp = os.path.join(root, f)
            sz = os.path.getsize(fp)
            if sz <= 1500 and "1KB" not in by_size:
                by_size["1KB"] = fp
            elif 5000 < sz < 15000 and "10KB" not in by_size:
                by_size["10KB"] = fp
            elif 50000 < sz < 200000 and "100KB" not in by_size:
                by_size["100KB"] = fp

    for label in ["1KB", "10KB", "100KB"]:
        if label in by_size:
            fp = by_size[label]
            t = time_op(lambda p=fp: open(p, "rb").read())
            print(f"3.  Cold cat {label}:             {t:.0f}ms")

    # 4. Warm read (now cached)
    for label in ["1KB", "10KB", "100KB"]:
        if label in by_size:
            fp = by_size[label]
            t = time_op(lambda p=fp: open(p, "rb").read(), n=20)
            print(f"4.  Warm cat {label} (p50/20x):    {t:.2f}ms")

    # 5. Warm getattr
    if deep:
        t = time_op(lambda: os.stat(deep), n=50)
        print(f"5.  Warm stat (p50/50x):        {t:.3f}ms")

    # 6. Background preload time from log
    with open("/tmp/clawfuse_cold.log") as f:
        for line in f:
            if "ready" in line:
                print(f"6.  Startup log: {line.strip()}")
            if "Background full load" in line:
                print(f"7.  Preload log: {line.strip()}")

    print()
    print("=" * 60)
    print("COLD START COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
