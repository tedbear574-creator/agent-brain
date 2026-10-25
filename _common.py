"""Small helpers for tickets.py: filename-safe writer ids, the home writer name,
and an append that rides out file contention."""
from __future__ import annotations

import os
import re
import time


def safe_name(name: str) -> str:
    """Keep a writer id/name usable as a filename (no slashes, spaces, etc.).

    Non-alphanumeric characters (outside ``-_.``) become ``-``; the result is
    stripped of leading/trailing ``-`` and falls back to ``"unknown"`` when it
    would be empty.
    """
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in name).strip("-")
    return cleaned or "unknown"


def home_writer_name() -> str:
    """The last segment of the home path (C:\\Users\\jsmith -> 'jsmith').

    Split on both slash styles so a Windows-style home path gives the same
    answer on any platform (os.path.basename would not). Returned raw — the
    caller applies its own sanitizer.
    """
    home = os.path.expanduser("~")
    parts = [p for p in re.split(r"[\\/]+", home) if p]
    return parts[-1] if parts else ""


def append_with_retry(path: str, line: str, attempts: int = 5) -> None:
    """Append one line to a file, riding out transient contention.

    Opening in append mode is atomic per line; a brief retry rides out the rare
    moment two local processes touch the same file at once.
    """
    for attempt in range(attempts):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
