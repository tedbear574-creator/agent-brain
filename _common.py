"""Small helpers shared by the three multi-writer CLIs (brain.py, tickets.py,
registers.py).

Each of these was copied into all three files; a copy that drifts is a bug
waiting to happen (two spool files that should never collide, a retry that
rides out contention in one tool but not another). One home, imported everywhere.
"""
from __future__ import annotations

import hashlib
import os
import re
import time


def safe_name(name: str, *, lower: bool = False, hash_on_change: bool = False) -> str:
    """Keep a writer id/name usable as a filename (no slashes, spaces, etc.).

    Non-alphanumeric characters (outside ``-_.``) become ``-``; the result is
    stripped of leading/trailing ``-`` and falls back to ``"unknown"`` when it
    would be empty.

    ``lower`` lowercases the input first, so an id reads identically everywhere
    ids are compared after lowercasing. ``hash_on_change`` appends a short hash
    of the (already-cased) original whenever sanitizing had to replace a
    character, so two distinct raw names ('a/b' and 'a b') never silently share
    one file. The stream tool (brain.py) turns both on; the ticket and register
    boards keep raw case and never hash.
    """
    raw = name.lower() if lower else name
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in raw).strip("-")
    if not cleaned:
        return "unknown"
    if hash_on_change and cleaned != raw:
        cleaned += "-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:4]
    return cleaned


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
