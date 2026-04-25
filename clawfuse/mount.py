"""ClawFUSE CLI entry point."""

from __future__ import annotations

import argparse
import logging
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

    # Register signal handlers for graceful shutdown
    def _shutdown(signum: int, frame: object) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        sync_result = lifecycle.pre_destroy()
        logger.info(
            "Shutdown complete: %d synced, %d failed",
            sync_result.files_synced,
            sync_result.files_failed,
        )
        sys.exit(0 if sync_result.files_failed == 0 else 1)

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
        fuse_ops.mount(config.mount_point, foreground=args.foreground)
    except ImportError:
        logger.error("fusepy not installed. Install with: pip install clawfuse[fuse]")
        sys.exit(1)
    except Exception as e:
        logger.error("FUSE mount error: %s", e)
        lifecycle.pre_destroy()
        sys.exit(1)


if __name__ == "__main__":
    main()
