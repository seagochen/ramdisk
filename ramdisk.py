#!/usr/bin/python3
"""Put AI-agent scratch data (Claude Code, Codex) on the per-user tmpfs. ./ramdisk.py --help"""
import sys

if sys.version_info < (3, 11):
    sys.exit("ramdisk.py needs Python 3.11+ (tomllib)")

from ramdisk_ai.cli import main  # noqa: E402

sys.exit(main())
