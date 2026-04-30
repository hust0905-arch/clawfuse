"""ClawFUSE CLI entry point."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from .config import Config
from .lifecycle import LifecycleManager


def main() -> None:
    """CLI entry point for ClawFUSE."""
    parser = argparse.ArgumentParser(description="ClawFUSE — Drive Kit FUSE mount")
    parser.add_argument("--config", help="JSON config file path (recommended)")
    parser.add_argument("--mount-point", help="FUSE mount point (overrides config/env)")
    parser.add_argument("--token-file", help="Access token file path (overrides env)")
    parser.add_argument("--root-folder", default=None, help="Drive Kit root folder ID")
    parser.add_argument("--foreground", action="store_true", help="Run in foreground")
    parser.add_argument("--log-level", default=None, help="Log level (DEBUG/INFO/WARNING/ERROR)")
    parser.add_argument("--allow-other", action="store_true", help="Allow other users to access the FUSE mount (requires root)")
    parser.add_argument("--nonempty", action="store_true", help="Allow mounting over a non-empty directory")
    args = parser.parse_args()

    # Build config: --config file > env vars > CLI overrides
    try:
        if args.config:
            config = Config.from_file(Path(args.config))
        else:
            config = Config.from_env()
    except Exception as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    # Apply CLI overrides on top
    if args.mount_point:
        config = Config(
            **{k: v for k, v in config.__dict__.items() if k != "mount_point"},
            mount_point=args.mount_point,
        )
    if args.token_file:
        config = Config(
            **{k: v for k, v in config.__dict__.items() if k != "token_file"},
            token_file=Path(args.token_file),
            token_string="",
        )
    if args.root_folder:
        config = Config(
            **{k: v for k, v in config.__dict__.items() if k not in ("root_folder", "cloud_folder")},
            root_folder=args.root_folder,
            cloud_folder=args.root_folder,
        )
    if args.log_level:
        config = Config(
            **{k: v for k, v in config.__dict__.items() if k != "log_level"},
            log_level=args.log_level,
        )
    if args.allow_other:
        config = Config(
            **{k: v for k, v in config.__dict__.items() if k != "allow_other"},
            allow_other=True,
        )
    if args.nonempty:
        config = Config(
            **{k: v for k, v in config.__dict__.items() if k != "nonempty"},
            nonempty=True,
        )

    # Setup logging
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("clawfuse")

    logger.info("Config: cloud_folder=%s, mount_point=%s", config.cloud_folder, config.mount_point)

    # Initialize lifecycle
    lifecycle = LifecycleManager(config)

    # Pre-start
    result = lifecycle.pre_start()
    if not result.success:
        logger.error("Mount failed: %s", result.error)
        sys.exit(1)

    logger.info(
        "ClawFUSE ready: %d files loaded in %.2fs at %s",
        result.file_count,
        result.load_time_seconds,
        result.mount_point,
    )

    # Register signal handlers — only set a flag, no HTTP or thread joins.
    # The FUSE loop will check this flag and exit cleanly.
    _shutdown_requested = False

    def _shutdown(signum: int, frame: object) -> None:
        nonlocal _shutdown_requested
        _shutdown_requested = True
        logger.info("Received signal %d, unmounting FUSE...", signum)
        # Non-blocking: fork fusermount so the FUSE main thread is NOT blocked.
        # os.system() is synchronous and causes deadlock — fusermount waits for
        # the kernel's DESTROY request, but DESTROY needs the FUSE main thread
        # to process, and the main thread is blocked in os.system().
        # subprocess.Popen returns immediately, letting the FUSE loop handle DESTROY.
        try:
            import subprocess
            subprocess.Popen(
                ["fusermount", "-u", config.mount_point],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Mount FUSE (blocking)
    fuse_ops = lifecycle.get_fuse_ops()
    if fuse_ops is None:
        logger.error("FUSE ops not available — components not initialized")
        sys.exit(1)

    try:
        from .fuse import ClawFUSE

        assert isinstance(fuse_ops, ClawFUSE)
        fuse_ops.mount(config.mount_point, foreground=args.foreground, allow_other=config.allow_other, nonempty=config.nonempty)
    except ImportError:
        logger.error("fusepy not installed. Install with: pip install clawfuse[fuse]")
        sys.exit(1)
    except Exception as e:
        logger.error("FUSE mount error: %s", e)
        lifecycle.pre_destroy()
        sys.exit(1)


if __name__ == "__main__":
    main()
