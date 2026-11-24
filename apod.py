#!/usr/bin/env python3
"""Entry point kept for compatibility: python apod.py daily|flashback [--dry-run]."""
import sys

from apod_bot.cli import main

if __name__ == "__main__":
    sys.exit(main())
